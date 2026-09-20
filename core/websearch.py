# -*- coding: utf-8 -*-
"""联网检索：搜索引擎（Bing / Baidu）+ 网页正文抽取。

现实约束（本机实测）：
    - Bing、Baidu 可达；Google、DuckDuckGo 超时不可用
    - 因此默认引擎顺序为 bing,baidu，任一失败自动降级

实现要点：
    - 只用标准库；显式声明 Accept-Encoding: identity，避免 gzip 解压依赖
    - 抓取限流：超时、最大字节数、正文最大字符数
    - 网页抽取：去 script/style/注释 → 块级标签转换行 → 去标签 → 反转义 → 收缩空行
"""
from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request

from .config import cfg

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MAX_BYTES = 1_500_000          # 单页最多读 1.5 MB
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg|template)[^>]*>.*?</\1>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BLOCK_RE = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|header|footer|table|ul|ol|pre|blockquote)\b[^>]*>", re.I)
_META_CHARSET_RE = re.compile(rb'<meta[^>]+charset=["\']?\s*([a-zA-Z0-9_\-]+)', re.I)
_BLANK_RE = re.compile(r"\n{3,}")

ENGINE_HOME = {
    "bing": "https://www.bing.com/search?q={q}&count=20&setlang=zh-CN&mkt=zh-CN",
    "baidu": "https://www.baidu.com/s?wd={q}&rn=20",
}


class WebError(RuntimeError):
    pass


def _get(url: str, timeout: int | None = None, referer: str | None = None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    # 关键：声明不压缩，省掉 gzip/brotli 处理
    req.add_header("Accept-Encoding", "identity")
    if referer:
        req.add_header("Referer", referer)
    timeout = timeout or cfg.get_int("WEB_TIMEOUT", 15)
    return urllib.request.urlopen(req, timeout=timeout)


def _decode(raw: bytes, content_type: str) -> str:
    charset = ""
    m = re.search(r"charset=([a-zA-Z0-9_\-]+)", content_type or "", re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m2 = _META_CHARSET_RE.search(raw[:4096])
        if m2:
            charset = m2.group(1).decode("ascii", "ignore")
    for cand in filter(None, [charset, "utf-8", "gb18030", "big5"]):
        try:
            return raw.decode(cand)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def strip_tags(fragment: str) -> str:
    frag = _SCRIPT_RE.sub(" ", fragment or "")
    frag = _COMMENT_RE.sub(" ", frag)
    frag = _BLOCK_RE.sub("\n", frag)
    frag = _TAG_RE.sub("", frag)
    frag = html.unescape(frag)
    frag = frag.replace("\u00a0", " ").replace("\u200b", "")
    frag = re.sub(r"[ \t\r\f\v]+", " ", frag)
    frag = re.sub(r" *\n *", "\n", frag)
    return _BLANK_RE.sub("\n\n", frag).strip()


# ---------------------------------------------------------------- 搜索结果解析

_BING_ITEM_RE = re.compile(r'<li class="b_algo[^"]*".*?</li>', re.S | re.I)
_BING_LINK_RE = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_BING_SNIP_RE = re.compile(r'<p[^>]*>(.*?)</p>', re.S | re.I)

_BAIDU_ITEM_RE = re.compile(r'<div[^>]+class="result[^"]*".*?(?=<div[^>]+class="result|<div id="page)', re.S | re.I)
_BAIDU_LINK_RE = re.compile(r'<h3[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_BAIDU_SNIP_RE = re.compile(r'<span[^>]+class="[^"]*(?:content-right|c-abstract|abstract)[^"]*"[^>]*>(.*?)</span>', re.S | re.I)


def _search_bing(query: str, limit: int) -> list[dict]:
    url = ENGINE_HOME["bing"].format(q=urllib.parse.quote_plus(query))
    out: list[dict] = []
    try:
        with _get(url) as resp:
            raw = resp.read(MAX_BYTES)
            page = _decode(raw, resp.headers.get("Content-Type", ""))
    except Exception:
        return out
    for block in _BING_ITEM_RE.findall(page):
        m = _BING_LINK_RE.search(block)
        if not m:
            continue
        link = html.unescape(m.group(1)).strip()
        if not link.startswith("http"):
            continue
        title = strip_tags(m.group(2))
        snip_m = _BING_SNIP_RE.search(block)
        snippet = strip_tags(snip_m.group(1)) if snip_m else ""
        if not title:
            continue
        out.append({"title": title, "url": link, "snippet": snippet[:400], "engine": "bing"})
        if len(out) >= limit:
            break
    return out


def _search_baidu(query: str, limit: int) -> list[dict]:
    url = ENGINE_HOME["baidu"].format(q=urllib.parse.quote_plus(query))
    out: list[dict] = []
    try:
        with _get(url) as resp:
            raw = resp.read(MAX_BYTES)
            page = _decode(raw, resp.headers.get("Content-Type", ""))
    except Exception:
        return out
    for block in _BAIDU_ITEM_RE.findall(page):
        m = _BAIDU_LINK_RE.search(block)
        if not m:
            continue
        link = html.unescape(m.group(1)).strip()
        if link.startswith("/"):
            link = "https://www.baidu.com" + link
        if not link.startswith("http"):
            continue
        title = strip_tags(m.group(2))
        snip_m = _BAIDU_SNIP_RE.search(block)
        snippet = strip_tags(snip_m.group(1)) if snip_m else strip_tags(block)[:300]
        if not title:
            continue
        out.append({"title": title, "url": link, "snippet": snippet[:400], "engine": "baidu"})
        if len(out) >= limit:
            break
    return out


_ENGINES = {"bing": _search_bing, "baidu": _search_baidu}


def search(query: str, limit: int = 10) -> list[dict]:
    """按配置的引擎顺序检索，失败自动降级；结果按 URL 去重。"""
    engines = cfg.get_list("WEB_ENGINES", "bing,baidu") or ["bing"]
    seen: set[str] = set()
    results: list[dict] = []
    for name in engines:
        fn = _ENGINES.get(name.lower())
        if not fn:
            continue
        for item in fn(query, limit):
            key = item["url"].split("#")[0]
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
        if len(results) >= limit:
            break
    return results[:limit]


# ---------------------------------------------------------------- 正文抽取

def fetch_text(url: str, max_chars: int | None = None) -> dict:
    """抓取网页并抽取正文。返回 {url, final_url, title, text, ok, error}。"""
    max_chars = max_chars or cfg.get_int("WEB_MAX_CHARS", 6000)
    out = {"url": url, "final_url": url, "title": "", "text": "", "ok": False, "error": ""}
    try:
        with _get(url, referer="https://www.bing.com/") as resp:
            ctype = resp.headers.get("Content-Type", "")
            if ctype and not any(k in ctype.lower() for k in ("html", "text", "xml")):
                out["error"] = f"非文本内容（{ctype.split(';')[0]}）"
                return out
            raw = resp.read(MAX_BYTES)
            out["final_url"] = resp.geturl() or url
            page = _decode(raw, ctype)
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}"
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}"
        return out

    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    if m:
        out["title"] = strip_tags(m.group(1))[:200]
    text = strip_tags(page)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（已截断）"
    out["text"] = text
    out["ok"] = len(text) > 120
    if not out["ok"] and not out["error"]:
        out["error"] = "正文过短，可能是动态渲染页面"
    return out


def gather(query: str, max_pages: int | None = None) -> dict:
    """联网检索 + 抓取正文的总入口。"""
    max_pages = max_pages or cfg.get_int("WEB_MAX_PAGES", 3)
    if not cfg.get_bool("WEB_SEARCH_ENABLED", True):
        return {"enabled": False, "results": [], "pages": [], "error": "联网检索已在设置中关闭"}
    results = search(query, limit=max(max_pages * 3, 8))
    pages: list[dict] = []
    for item in results:
        if len(pages) >= max_pages:
            break
        page = fetch_text(item["url"])
        if page["ok"]:
            page["snippet"] = item.get("snippet", "")
            page["engine"] = item.get("engine", "")
            page["title"] = page["title"] or item.get("title", "")
            pages.append(page)
    return {"enabled": True, "results": results, "pages": pages, "error": "" if pages else "未抓取到有效正文"}
