# -*- coding: utf-8 -*-
"""本地运维助手 · 服务入口。

纯 Python 标准库实现（http.server + urllib + sqlite3 + re + json）：
    - 零第三方依赖，免安装、秒启动
    - 仅监听 127.0.0.1（不对外暴露）
    - 前端为单页原生 HTML/CSS/JS，前端库已本地化，页面运行时不依赖外网

启动：python app.py
访问：http://127.0.0.1:8765
"""
from __future__ import annotations

import json
import mimetypes
import os
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import ingest, llm, prompts, store, websearch          # noqa: E402
from core.config import cfg                                      # noqa: E402
from core import kb as kb_mod                                    # noqa: E402

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/manifest+json", ".webmanifest")

LOG_PATH = cfg.log_dir / "app.log"
_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with _log_lock:
        try:
            cfg.log_dir.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    try:
        print(line, flush=True)
    except Exception:
        pass


# ================================================================ RAG 编排

def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + " …（已截断）"


def build_kb_context(result: kb_mod.SearchResult) -> str:
    if not result.hits:
        return ""
    lines = [f"【知识库检索结果】命中 {len(result.hits)} 条，置信度 {result.confidence:.2f}", ""]
    for h in result.hits:
        head = f"[{h.rank}] [{h.doc_id}] {h.title}"
        if h.heading:
            head += f" › {h.heading}"
        head += f"  （{h.rel}）"
        lines.append(head)
        lines.append(h.body)
        lines.append("")
    return "\n".join(lines)


def build_web_context(web: dict) -> str:
    pages = web.get("pages") or []
    if not pages:
        return ""
    lines = ["【联网检索资料】（来自 " + "、".join(sorted({p.get("engine", "web") for p in pages})) + "）", ""]
    for i, p in enumerate(pages, 1):
        lines.append(f"[{i}] {p.get('title') or p.get('url')}")
        lines.append(f"    来源：{p.get('url')}")
        lines.append(p.get("text", "")[: cfg.get_int("WEB_MAX_CHARS", 6000)])
        lines.append("")
    return "\n".join(lines)


def decide_route(question: str, mode: str, kb_result: kb_mod.SearchResult) -> str:
    """返回 kb / web / both / none。"""
    threshold = cfg.get_float("KB_SCORE_THRESHOLD", 0.5)
    if mode == "kb":
        return "kb" if kb_result.confidence >= threshold or kb_result.hits else "none"
    if mode == "web":
        return "web" if cfg.get_bool("WEB_SEARCH_ENABLED", True) else "none"
    if kb_result.confidence >= threshold:
        return "kb"
    return "web" if cfg.get_bool("WEB_SEARCH_ENABLED", True) else "none"


def _closest_note(kb_result: kb_mod.SearchResult) -> str:
    if not kb_result.hits:
        return ""
    items = [f"- `[{h.doc_id}]` {h.title}" + (f" › {h.heading}" if h.heading else "")
             for h in kb_result.hits[:3]]
    return "最接近的几篇（相似度不足，仅供参考）：\n" + "\n".join(items)


# ================================================================ SSE

class SSE:
    """Server-Sent Events 写出器。"""

    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self.h = handler
        self.closed = False

    def headers(self) -> None:
        self.h.send_response(200)
        self.h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.h.send_header("Cache-Control", "no-cache, no-transform")
        self.h.send_header("X-Accel-Buffering", "no")
        self.h.send_header("Connection", "close")
        self.h.end_headers()

    def send(self, event: str, data) -> None:
        if self.closed:
            return
        if not isinstance(data, str):
            data = json.dumps(data, ensure_ascii=False)
        payload = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        try:
            self.h.wfile.write(payload)
            self.h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.closed = True

    def comment(self, text: str = "") -> None:
        self.send("", text) if False else None
        try:
            self.h.wfile.write(f": {text}\n\n".encode("utf-8"))
            self.h.wfile.flush()
        except Exception:
            self.closed = True


# ================================================================ Handler

class Handler(BaseHTTPRequestHandler):
    server_version = "OpsAssistant/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础设施 ----------
    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        return

    def _no_delay(self) -> None:
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": msg}, code)

    @staticmethod
    def _refresh_config() -> None:
        """外部改过 .env 就热重载，免去手工重启。"""
        try:
            if cfg.maybe_reload():
                log("检测到 .env 变更，配置已自动重载")
        except Exception:  # noqa: BLE001
            pass

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return {}

    def _query(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802
        self._no_delay()
        self._refresh_config()
        try:
            self._route_get()
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            log("GET 异常 " + traceback.format_exc())
            self._err(f"服务端异常：{e}", 500)

    def do_POST(self) -> None:  # noqa: N802
        self._no_delay()
        self._refresh_config()
        try:
            self._route_post()
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            log("POST 异常 " + traceback.format_exc())
            self._err(f"服务端异常：{e}", 500)

    def do_DELETE(self) -> None:  # noqa: N802
        self._no_delay()
        self._refresh_config()
        try:
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/api/sessions/"):
                store.delete_session(path.rsplit("/", 1)[-1])
                return self._json({"ok": True})
            self._err("未知接口", 404)
        except Exception as e:  # noqa: BLE001
            self._err(str(e), 500)

    # ---------- GET ----------
    def _route_get(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        q = self._query()

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])

        if path == "/api/health":
            return self._json({
                "ok": True,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "kb": kb_mod.kb_index.stats(),
                "chat": store.stats(),
                "llm": {"model": cfg.get("MODEL"), "api_key_set": bool(cfg.api_key)},
                "web": {"enabled": cfg.get_bool("WEB_SEARCH_ENABLED", True),
                        "engines": cfg.get_list("WEB_ENGINES", "bing,baidu")},
            })

        if path == "/api/settings":
            return self._json({"ok": True, "settings": cfg.public()})

        if path == "/api/kb/stats":
            return self._json({"ok": True, "stats": kb_mod.kb_index.stats()})

        if path == "/api/kb/volumes":
            return self._json({"ok": True, "volumes": kb_mod.kb_index.list_volumes()})

        if path == "/api/kb/docs":
            return self._json({"ok": True, "docs": kb_mod.kb_index.list_docs()})

        if path == "/api/kb/search":
            res = kb_mod.kb_index.search(q.get("q", ""), top_k=int(q.get("top_k") or cfg.get_int("TOP_K", 5)))
            return self._json({
                "ok": True,
                "confidence": res.confidence,
                "tokens": res.tokens,
                "detail": res.detail,
                "hits": [h.to_dict() for h in res.hits],
            })

        if path == "/api/kb/doc":
            doc = kb_mod.kb_index.get_doc(q.get("rel", ""))
            if not doc:
                return self._err("文档不存在", 404)
            return self._json({"ok": True, "doc": doc})

        if path == "/api/doc":
            # 与 kb/doc 等价，便于前端引用
            doc = kb_mod.kb_index.get_doc(q.get("rel", ""))
            if not doc:
                return self._err("文档不存在", 404)
            return self._json({"ok": True, "doc": doc})

        if path == "/api/sessions":
            return self._json({"ok": True, "sessions": store.list_sessions()})

        if path.startswith("/api/sessions/") and path.endswith("/messages"):
            sid = path[len("/api/sessions/"):-len("/messages")]
            if not store.get_session(sid):
                return self._err("会话不存在", 404)
            return self._json({"ok": True, "messages": store.get_messages(sid)})

        if path == "/api/search":
            return self._json({"ok": True, "results": store.search_messages(q.get("q", ""))})

        if path == "/api/drafts":
            return self._json({"ok": True, "drafts": ingest.list_drafts()})

        if path == "/api/drafts/read":
            d = ingest.read_draft(q.get("name", ""))
            if not d:
                return self._err("草稿不存在", 404)
            return self._json({"ok": True, "draft": d})

        if path == "/api/llm/health":
            return self._json({"ok": True, "result": llm.health()})

        self._err("未知接口", 404)

    # ---------- POST ----------
    def _route_post(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        body = self._read_json()

        if path == "/api/chat":
            return self._chat(body)

        if path == "/api/sessions":
            s = store.create_session(body.get("title") or "新对话")
            return self._json({"ok": True, "session": s})

        if path.startswith("/api/sessions/") and path.endswith("/rename"):
            sid = path[len("/api/sessions/"):-len("/rename")]
            store.rename_session(sid, body.get("title") or "未命名")
            return self._json({"ok": True})

        if path == "/api/settings":
            updates = {k: str(v) for k, v in (body.get("updates") or {}).items()}
            allowed = {"MODEL", "TEMPERATURE", "MAX_TOKENS", "HISTORY_TURNS", "KB_DIR", "TOP_K",
                       "KB_SCORE_THRESHOLD", "MAX_CONTEXT_CHARS", "WEB_SEARCH_ENABLED", "WEB_ENGINES",
                       "WEB_MAX_PAGES", "WEB_TIMEOUT", "AUTO_INGEST", "WEB_MAX_CHARS"}
            updates = {k: v for k, v in updates.items() if k in allowed}
            if body.get("api_key"):
                updates["DEEPSEEK_API_KEY"] = str(body["api_key"]).strip()
            if body.get("base_url"):
                updates["DEEPSEEK_BASE_URL"] = str(body["base_url"]).strip().rstrip("/")
            cfg.set_many(updates)
            return self._json({"ok": True, "settings": cfg.public()})

        if path == "/api/kb/reindex":
            res = kb_mod.kb_index.build(force=True)
            return self._json({"ok": True, "result": res})

        if path == "/api/drafts/delete":
            ok = ingest.delete_draft(body.get("name", ""))
            return self._json({"ok": ok})

        if path == "/api/drafts/enrich":
            return self._json(ingest.enrich_draft(body.get("name", "")))

        if path == "/api/drafts/promote":
            res = ingest.promote(
                name=body.get("name", ""),
                volume=body.get("volume", ""),
                doc_id=body.get("doc_id", ""),
                title=body.get("title", ""),
                tags=body.get("tags", ""),
                level=body.get("level", "L2"),
                register_index=bool(body.get("register_index", True)),
            )
            return self._json(res, 200 if res.get("ok") else 400)

        if path == "/api/shutdown":
            self._json({"ok": True, "message": "服务即将停止"})
            threading.Timer(0.6, lambda: os._exit(0)).start()
            return

        self._err("未知接口", 404)

    # ---------- 静态文件 ----------
    def _static(self, rel: str) -> None:
        rel = rel.split("?")[0].lstrip("/")
        if not rel:
            rel = "index.html"
        base = cfg.static_dir.resolve()
        target = (base / rel).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return self._err("非法路径", 403)
        if not target.exists() or target.is_dir():
            return self._err("文件不存在", 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        # 缓存策略：自带代码（app.css/app.js/index.html）一律不缓存，
        # 避免改完代码后浏览器还跑旧版本；第三方 vendor 库内容稳定，长缓存。
        is_vendor = rel.replace("\\", "/").startswith("vendor/")
        if is_vendor and target.suffix != ".html":
            cache = "public, max-age=604800"
        else:
            cache = "no-cache, must-revalidate"
        self._send(200, target.read_bytes(), ctype, cache)

    # ---------- 对话（SSE） ----------
    def _chat(self, body: dict) -> None:
        question = (body.get("message") or "").strip()
        sid = body.get("session_id") or ""
        mode = (body.get("mode") or "auto").lower()
        use_ingest = body.get("ingest", True)

        sse = SSE(self)
        sse.headers()

        if not question:
            sse.send("error", {"message": "问题为空"})
            sse.send("done", {})
            self.close_connection = True
            return

        if not sid or not store.get_session(sid):
            sid = store.create_session(question[:20])["id"]
            sse.send("session", {"id": sid})

        # 1) 先取历史（不含本轮），再落库本轮用户消息
        history = store.history_for_llm(sid, turns=cfg.get_int("HISTORY_TURNS", 6))
        store.add_message(sid, "user", question)

        t0 = time.time()
        kb_result = None
        web_result = None
        route = "none"

        try:
            sse.send("stage", {"stage": "searching_kb", "text": "正在检索本地知识库…"})
            kb_result = kb_mod.kb_index.search(question)
            route = decide_route(question, mode, kb_result)
            sse.send("meta", {
                "route": route,
                "confidence": kb_result.confidence,
                "detail": kb_result.detail,
                "tokens": kb_result.tokens,
                "hits": [h.to_dict() for h in kb_result.hits],
                "kb_hit": kb_result.confidence >= cfg.get_float("KB_SCORE_THRESHOLD", 0.5),
            })

            if route in ("web", "both"):
                sse.send("stage", {"stage": "searching_web", "text": "本地知识库未覆盖，正在联网检索…"})
                web_result = websearch.gather(question)
                pages = web_result.get("pages") or []
                if pages:
                    sse.send("web", {
                        "results": (web_result.get("results") or [])[:6],
                        "pages": [{"title": p.get("title"), "url": p.get("final_url") or p.get("url"),
                                   "engine": p.get("engine"), "chars": len(p.get("text") or "")} for p in pages],
                    })
                else:
                    sse.send("stage", {"stage": "web_empty",
                                       "text": f"联网检索未取得有效内容（{web_result.get('error') or '未知原因'}）"})

            # 2) 组装上下文
            ctx_parts: list[str] = []
            if kb_result and kb_result.hits:
                ctx_parts.append(build_kb_context(kb_result))
            if web_result and web_result.get("pages"):
                ctx_parts.append(build_web_context(web_result))
            context = _clip("\n\n".join(p for p in ctx_parts if p), cfg.get_int("MAX_CONTEXT_CHARS", 9000))

            # 3) 决定系统提示词与实际回答路径
            has_kb = bool(kb_result and kb_result.hits)
            has_web = bool(web_result and web_result.get("pages"))
            if route == "both" or (has_kb and has_web):
                system = prompts.SYSTEM_BOTH
                answer_route = "both"
            elif has_web and (route == "web" or not has_kb):
                system = prompts.SYSTEM_WEB
                answer_route = "web"
            elif has_kb:
                system = prompts.SYSTEM_KB
                answer_route = "kb"
            else:
                answer_route = "none"
                system = ""

            full = ""
            usage: dict = {}

            if answer_route == "none":
                # 无可用上下文：直接给出可操作的降级说明，不再调用模型
                text = prompts.NO_HIT_MESSAGE.format(closest=_closest_note(kb_result) if kb_result else "")
                for i in range(0, len(text), 60):
                    sse.send("delta", {"t": text[i:i + 60]})
                    time.sleep(0.01)
                full = text
            else:
                msgs = [{"role": "system", "content": system}] + history
                msgs.append({"role": "user", "content":
                             f"{context}\n\n=====================\n\n【我的问题】\n{question}"})
                sse.send("stage", {"stage": "answering", "text": "正在生成回答…"})
                full, usage = llm.stream_chat(msgs, on_delta=lambda t: sse.send("delta", {"t": t}))

            elapsed = round(time.time() - t0, 2)
            sources = [h.to_dict() for h in (kb_result.hits if kb_result else [])]
            web_sources = [{"title": p.get("title"), "url": p.get("final_url") or p.get("url"),
                            "engine": p.get("engine")} for p in ((web_result or {}).get("pages") or [])]

            meta = {
                "route": answer_route,
                "confidence": kb_result.confidence if kb_result else 0.0,
                "sources": sources,
                "web_sources": web_sources,
                "usage": usage,
                "elapsed": elapsed,
            }
            msg_id = store.add_message(sid, "assistant", full, meta)

            draft = {"saved": False}
            if answer_route in ("web", "both") and use_ingest and cfg.get("AUTO_INGEST") != "off":
                try:
                    draft = ingest.save_draft(question, full, web_sources)
                except Exception as e:  # noqa: BLE001
                    log("草稿写入失败：" + traceback.format_exc())
                    draft = {"saved": False, "reason": str(e)}

            sse.send("sources", {"kb": sources, "web": web_sources,
                                 "route": answer_route, "confidence": meta["confidence"]})
            sse.send("done", {
                "message_id": msg_id,
                "session_id": sid,
                "usage": usage,
                "elapsed": elapsed,
                "route": answer_route,
                "draft": draft,
                "title": (store.get_session(sid) or {}).get("title", ""),
            })
            log(f"chat ok sid={sid} route={answer_route} conf={meta['confidence']} {elapsed}s "
                f"chars={len(full)}")
        except llm.LLMError as e:
            log(f"LLM 错误：{e}")
            sse.send("error", {"message": str(e)})
            sse.send("done", {"error": True})
        except Exception as e:  # noqa: BLE001
            log("chat 异常 " + traceback.format_exc())
            sse.send("error", {"message": f"{type(e).__name__}: {e}"})
            sse.send("done", {"error": True})
        finally:
            self.close_connection = True


# ================================================================ 启动

def build_index_on_start() -> None:
    try:
        t0 = time.time()
        res = kb_mod.kb_index.build(force=False)
        log(f"知识库索引就绪：{res.get('docs')} 篇 / {res.get('chunks')} 块，"
            f"{'重建' if res.get('rebuilt') else '复用缓存'}，{time.time() - t0:.2f}s")
    except Exception:  # noqa: BLE001
        log("索引构建失败：\n" + traceback.format_exc())


def probe_existing_server(host: str, port: int) -> str:
    """探测目标端口上是否已经有服务。返回 "" / "ours" / "other"。

    存在的意义：Windows 上 SO_REUSEADDR 允许两个进程同时 bind 同一端口。
    若不先探测，第二个实例会"启动成功"，但请求被随机分流给旧实例 —— 表现
    出来就是"明明改了配置，却像没生效"。
    """
    try:
        with socket.create_connection((host, port), timeout=0.8) as sock:
            sock.sendall(
                f"GET /api/health HTTP/1.0\r\nHost: {host}:{port}\r\n"
                "Connection: close\r\n\r\n".encode()
            )
            sock.settimeout(1.5)
            buf = b""
            while len(buf) < 8192:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return ""
    body = buf.decode("utf-8", "replace")
    return "ours" if ('"kb"' in body and '"chunks"' in body) else "other"


def main() -> None:
    host = cfg.get("HOST") or "127.0.0.1"
    port = cfg.get_int("PORT", 8765)
    url = f"http://{host}:{port}/"

    # 先探测再绑：宁可明确失败，也不要两个实例同时占一个端口
    existing = probe_existing_server(host, port)
    if existing:
        cfg.ensure_dirs()
        if existing == "ours":
            log(f"[!] 端口 {port} 上已有一个本助手实例在运行，本次启动中止。")
            log(f"    直接打开 {url} 即可；要重启请先运行 stop.bat")
            return
        log(f"[!] 端口 {port} 已被其他程序占用，启动中止。")
        log("    请先释放该端口，或修改 .env 里的 PORT 后重试。")
        sys.exit(2)

    cfg.ensure_dirs()
    store.init()

    log("=" * 66)
    log("本地运维知识库助手 启动中…")
    log(f"知识库目录：{cfg.kb_dir}（存在={cfg.kb_dir.exists()}）")
    log(f"模型：{cfg.get('MODEL')}   API Key：{'已配置' if cfg.api_key else '未配置'}")
    log(f"联网检索：{'开启' if cfg.get_bool('WEB_SEARCH_ENABLED', True) else '关闭'}  "
        f"引擎：{','.join(cfg.get_list('WEB_ENGINES', 'bing,baidu'))}")

    build_index_on_start()

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log(f"服务已就绪 → {url}")
    log("=" * 66)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到中断信号，正在退出…")
    finally:
        httpd.server_close()
        kb_mod.kb_index.close()
        log("已退出。")


if __name__ == "__main__":
    main()
