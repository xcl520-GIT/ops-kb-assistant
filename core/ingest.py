# -*- coding: utf-8 -*-
"""知识库回写（Ingest）。

安全红线（重要）：
    - **绝不修改/覆盖**知识库中已有的正式文档；
    - 联网得到的答案一律先写入 ``<KB>/_inbox/``（待审核区），
      由用户在界面上显式点击「采纳入库」后，才移动到正式卷目录；
    - 采纳时可选择「同时登记到 KB-总索引」，登记前自动备份总索引。

流程：
    联网答案 → save_draft() → _inbox/xxx.md（status: 待审核）
             → promote()   → <卷目录>/<ID>-<标题>.md（status: 生效）
             → 重建检索索引
"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from .config import cfg
from . import kb as kb_mod
from . import llm

INBOX_STATUS = "待审核-来自联网检索"
FINAL_STATUS = "生效"


# ---------------------------------------------------------------- 工具

def _slug(title: str, limit: int = 28) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "", title or "").strip()
    s = re.sub(r"\s+", "-", s)
    return (s[:limit] or "untitled").strip("-")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _fm(meta: dict) -> str:
    def fmt(v) -> str:
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(str(x) for x in v) + "]"
        return str(v)

    order = ["id", "title", "volume", "tags", "aliases", "level",
             "prerequisites", "related", "status", "source", "updated"]
    lines = ["---"]
    for key in order:
        if key in meta:
            lines.append(f"{key}: {fmt(meta[key])}")
    for key, val in meta.items():
        if key not in order:
            lines.append(f"{key}: {fmt(val)}")
    lines.append("---")
    return "\n".join(lines)


def _ensure_dirs() -> None:
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 元数据提炼

_META_PROMPT = """你是知识库管理员。根据下面这组"问题 + 回答"，提炼文档元数据。

要求：
1. title：不超过 20 字的中文标题，描述技术主题（不要照抄问题）
2. tags：6~10 个技术标签，中英混合
3. aliases：15~30 个检索别名，必须包含：同义词、英文全称与缩写、中文口语化说法、典型报错关键字
4. level：L1/L2/L3/L4 之一（L1 入门、L2 基础、L3 进阶、L4 专家）
5. summary：一句话概括（不超过 60 字）

只输出 JSON，不要任何解释或代码块标记，格式：
{"title":"", "tags":[], "aliases":[], "level":"L2", "summary":""}

问题：<<Q>>

回答（节选）：<<A>>
"""
# 注意：上面模板里的 JSON 花括号不能直接配合 str.format() 使用
#（会被当成格式占位符 → KeyError），因此统一用 <<Q>> / <<A>> 占位符替换。


def extract_meta(question: str, answer: str) -> dict:
    """用 LLM 提炼标题/标签/别名；失败则退回启发式。"""
    fallback = {
        "title": (question or "未命名主题").strip()[:20],
        "tags": ["网络检索"],
        "aliases": [question.strip()[:30]] if question else [],
        "level": "L2",
        "summary": (answer or "").strip().replace("\n", " ")[:60],
    }
    if not cfg.api_key:
        return fallback
    prompt = (_META_PROMPT
              .replace("<<Q>>", question[:500])
              .replace("<<A>>", (answer or "")[:2500]))
    try:
        raw = llm.complete(
            [{"role": "user", "content": prompt}],
            max_tokens=700, temperature=0.2,
        )
        raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return fallback
        obj = json.loads(m.group(0))
        out = {
            "title": str(obj.get("title") or fallback["title"])[:40],
            "tags": [str(x) for x in (obj.get("tags") or [])][:10] or fallback["tags"],
            "aliases": [str(x) for x in (obj.get("aliases") or [])][:30] or fallback["aliases"],
            "level": str(obj.get("level") or "L2")[:4],
            "summary": str(obj.get("summary") or fallback["summary"])[:120],
        }
        return out
    except Exception as e:  # noqa: BLE001  元数据提炼失败不应阻断主流程
        print(f"[warn] AI 元数据提炼失败，已回退到启发式：{type(e).__name__}: {e}", flush=True)
        return fallback


# ---------------------------------------------------------------- 草稿

def _demote_headings(text: str, levels: int = 2) -> str:
    """把答案里的 markdown 标题整体降级，避免与草稿自身的章节编号打架。"""
    out_lines = []
    in_fence = False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if not in_fence:
            m = re.match(r"^(#{1,6})\s+(.*)$", line)
            if m:
                n = min(6, len(m.group(1)) + levels)
                line = "#" * n + " " + m.group(2)
        out_lines.append(line)
    return "\n".join(out_lines)


def save_draft(question: str, answer: str, sources: list[dict],
               meta: dict | None = None, mode: str | None = None) -> dict:
    """把联网得到的答案写成待审核草稿，返回草稿信息。"""
    mode = (mode or cfg.get("AUTO_INGEST") or "inbox").lower()
    if mode == "off":
        return {"saved": False, "reason": "回写已关闭（AUTO_INGEST=off）"}

    _ensure_dirs()
    meta = meta or {}
    title = meta.get("title") or (question or "未命名主题").strip()[:24]
    slug = _slug(title)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = cfg.inbox_dir / f"{stamp}-{slug}.md"

    urls = [s.get("url", "") for s in (sources or []) if s.get("url")]
    body_lines = [
        f"# {title}",
        "",
        f"> **来源**：联网检索（{'、'.join(sorted({s.get('engine', 'web') for s in sources}) or ['web'])}）",
        f"> **原始问题**：{question}",
        f"> **生成时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> **状态**：{INBOX_STATUS} —— 请核对内容后决定是否采纳入库",
        "",
        "---",
        "",
        "## 一、问题",
        "",
        question or "",
        "",
        "## 二、回答（AI 整理，需人工核对）",
        "",
        _demote_headings(answer),
        "",
        "## 三、参考来源",
        "",
    ]
    for i, s in enumerate(sources or [], 1):
        body_lines.append(f"{i}. [{s.get('title') or s.get('url')}]({s.get('url')})")
    body_lines += [
        "",
        "---",
        "",
        "## 四、采纳入库检查清单",
        "",
        "- [ ] 内容准确性已人工核对（AI 可能出错）",
        "- [ ] 补充「知识边界与常见误区」段（适用边界 / 常见误区 / 经验教训）",
        "- [ ] 修正为知识库统一的十段式结构",
        "- [ ] 补全 tags / aliases / related",
        "",
    ]

    fm = _fm({
        "id": f"TMP-{stamp[-6:]}",
        "title": title,
        "volume": "_inbox",
        "tags": meta.get("tags") or ["网络检索"],
        "aliases": meta.get("aliases") or [],
        "level": meta.get("level") or "L2",
        "prerequisites": [],
        "related": [],
        "status": INBOX_STATUS,
        "source": urls[:5],
        "updated": _today(),
    })
    path.write_text(fm + "\n\n" + "\n".join(body_lines), encoding="utf-8")

    return {
        "saved": True,
        "path": str(path),
        "name": path.name,
        "title": title,
        "mode": mode,
        "is_draft": True,
    }


def list_drafts() -> list[dict]:
    _ensure_dirs()
    out: list[dict] = []
    for p in sorted(cfg.inbox_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = kb_mod.parse_front_matter(text)
        out.append({
            "name": p.name,
            "title": kb_mod.as_text(meta.get("title")) or p.stem,
            "status": kb_mod.as_text(meta.get("status")),
            "level": kb_mod.as_text(meta.get("level")),
            "tags": kb_mod.as_text(meta.get("tags")),
            "aliases": kb_mod.as_text(meta.get("aliases")),
            "source": kb_mod.as_text(meta.get("source")),
            "updated": kb_mod.as_text(meta.get("updated")),
            "size": p.stat().st_size,
            "mtime": p.stat().st_mtime,
            "preview": body.strip()[:400],
        })
    return out


def read_draft(name: str) -> dict | None:
    path = (cfg.inbox_dir / name).resolve()
    try:
        path.relative_to(cfg.inbox_dir.resolve())
    except ValueError:
        return None
    if not path.exists() or path.suffix.lower() != ".md":
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = kb_mod.parse_front_matter(text)
    return {"name": path.name, "meta": {k: kb_mod.as_text(v) for k, v in meta.items()},
            "raw": text, "body": body, "size": len(text)}


def enrich_draft(name: str) -> dict:
    """用 LLM 重新提炼草稿的 title/tags/aliases/level，并写回 front-matter（正文不动）。"""
    d = read_draft(name)
    if not d:
        return {"ok": False, "error": "草稿不存在"}

    body = d.get("body") or ""
    question = ""
    m = re.search(r"##\s*一、问题\s*\n+(.*?)(?=\n##\s)", body, re.S)
    if m:
        question = m.group(1).strip()
    answer = ""
    m = re.search(r"##\s*二、回答[^\n]*\n+(.*?)(?=\n##\s)", body, re.S)
    if m:
        answer = m.group(1).strip()
    if not question:
        question = d["meta"].get("title", "")
    if not answer:
        answer = body[:3000]

    meta = extract_meta(question, answer)
    new_fm = _fm({
        "id": d["meta"].get("id") or "TMP",
        "title": meta["title"],
        "volume": "_inbox",
        "tags": meta["tags"],
        "aliases": meta["aliases"],
        "level": meta["level"],
        "prerequisites": [],
        "related": [],
        "status": INBOX_STATUS,
        "source": [s for s in (d["meta"].get("source") or "").split(",") if s.strip()][:5],
        "updated": _today(),
    })
    path = cfg.inbox_dir / name
    path.write_text(new_fm + "\n\n" + body.lstrip("\n"), encoding="utf-8")
    return {"ok": True, "meta": meta, "summary": meta.get("summary", "")}


def delete_draft(name: str) -> bool:
    path = (cfg.inbox_dir / name).resolve()
    try:
        path.relative_to(cfg.inbox_dir.resolve())
    except ValueError:
        return False
    if path.exists() and path.suffix.lower() == ".md":
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------- 采纳入库

def _register_in_index(volume: str, doc_id: str, title: str, tags: str) -> dict:
    """把新文档登记到 KB-总索引.md 对应卷的表格里（写前自动备份）。"""
    index_path = cfg.kb_dir / "00-导航" / "KB-总索引.md"
    if not index_path.exists():
        return {"ok": False, "reason": "未找到 KB-总索引.md"}

    letter = ""
    m = re.match(r"^([A-Za-z0-9]{1,3})", volume)
    if m:
        letter = m.group(1).upper()
    if not letter:
        return {"ok": False, "reason": "无法从卷名推断编号字母"}

    text = index_path.read_text(encoding="utf-8", errors="replace")
    head_re = re.compile(rf"^##\s+{re.escape(letter)}\s*·.*$", re.M)
    hm = head_re.search(text)
    if not hm:
        return {"ok": False, "reason": f"总索引中未找到 {letter} 卷的章节"}

    nxt = text.find("\n## ", hm.end())
    section_end = len(text) if nxt == -1 else nxt
    section = text[hm.start():section_end]

    row = f"| {doc_id} | {title} | ✅ | {tags} |\n"
    if f"| {doc_id} |" in section:
        return {"ok": False, "reason": f"总索引中已存在 {doc_id}"}

    # 找到该卷表格的最后一行（以 | 开头），把新行插在它后面
    rows = list(re.finditer(r"^\|.*\|\s*$", section, re.M))
    if not rows:
        return {"ok": False, "reason": "该卷章节内未找到表格"}
    last = rows[-1]
    insert_at = hm.start() + last.end()

    backup_dir = cfg.data_dir / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"KB-总索引.{datetime.now().strftime('%Y%m%d-%H%M%S')}.md.bak"
    shutil.copy2(index_path, backup)

    new_text = text[:insert_at] + "\n" + row.rstrip("\n") + text[insert_at:]
    # 顺带把卷标题里的篇数 +1（形如「（12 篇）」）
    cnt_re = re.compile(rf"^(##\s+{re.escape(letter)}\s*·[^\n（(]*[（(])(\d+)(\s*篇)")
    new_text = cnt_re.sub(lambda mm: f"{mm.group(1)}{int(mm.group(2)) + 1}{mm.group(3)}", new_text, count=1)
    index_path.write_text(new_text, encoding="utf-8")
    return {"ok": True, "backup": str(backup)}


def promote(name: str, volume: str, doc_id: str, title: str,
            tags: str = "", level: str = "L2", register_index: bool = True) -> dict:
    """把草稿从 _inbox 移动到正式卷目录，可选登记到总索引，最后重建索引。"""
    src = (cfg.inbox_dir / name).resolve()
    try:
        src.relative_to(cfg.inbox_dir.resolve())
    except ValueError:
        return {"ok": False, "error": "非法的草稿名"}
    if not src.exists():
        return {"ok": False, "error": "草稿不存在"}

    volume = (volume or "").strip().strip("/\\")
    if not volume or volume.startswith("_"):
        return {"ok": False, "error": "请选择目标卷"}
    vol_dir = (cfg.kb_dir / volume).resolve()
    try:
        vol_dir.relative_to(cfg.kb_dir.resolve())
    except ValueError:
        return {"ok": False, "error": "非法的卷名"}
    if not vol_dir.exists():
        return {"ok": False, "error": f"卷目录不存在：{volume}"}

    doc_id = (doc_id or "").strip()
    title = (title or "").strip() or "未命名主题"
    if not re.fullmatch(r"[A-Za-z]{1,3}\d{2,3}", doc_id):
        return {"ok": False, "error": "ID 需形如 H11 / K08（1~3 个字母 + 2~3 位数字）"}

    dest = vol_dir / f"{doc_id}-{_slug(title, 40)}.md"
    if dest.exists():
        return {"ok": False, "error": f"目标文件已存在：{dest.name}"}

    raw = src.read_text(encoding="utf-8", errors="replace")
    meta, body = kb_mod.parse_front_matter(raw)
    meta.update({
        "id": doc_id,
        "title": title,
        "volume": volume,
        "status": FINAL_STATUS,
        "updated": _today(),
    })
    if tags:
        meta["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if level:
        meta["level"] = level
    dest.write_text(_fm(meta) + "\n\n" + body.lstrip("\n"), encoding="utf-8")
    src.unlink()

    reg = {"ok": False, "reason": "未登记"}
    if register_index:
        try:
            reg = _register_in_index(volume, doc_id, title, kb_mod.as_text(meta.get("tags")))
        except Exception as e:  # noqa: BLE001
            reg = {"ok": False, "reason": f"登记失败：{e}"}

    rebuilt = kb_mod.kb_index.build(force=True)
    return {
        "ok": True,
        "doc_id": doc_id,
        "dest": str(dest),
        "rel": f"{volume}/{dest.name}",
        "index_registered": reg,
        "reindex": rebuilt,
    }
