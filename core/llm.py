# -*- coding: utf-8 -*-
"""DeepSeek（OpenAI 兼容）客户端：流式对话 + 非流式补全。

只用标准库 urllib，避免任何第三方依赖。
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

from .config import cfg


class LLMError(RuntimeError):
    pass


def _messages_payload(messages: list[dict], stream: bool, **over) -> dict:
    payload = {
        "model": cfg.get("MODEL") or "deepseek-chat",
        "messages": messages,
        "stream": stream,
        "temperature": cfg.get_float("TEMPERATURE", 0.3),
        "max_tokens": cfg.get_int("MAX_TOKENS", 2048),
    }
    payload.update(over)
    return payload


def _request(path: str, payload: dict, timeout: int | None = None):
    url = f"{cfg.base_url}{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {cfg.api_key}")
    return urllib.request.urlopen(req, timeout=timeout or cfg.get_int("LLM_TIMEOUT", 180))


def _explain_http_error(err: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        raw = err.read().decode("utf-8", "replace")
        obj = json.loads(raw)
        detail = obj.get("error", {}).get("message") or raw[:300]
    except Exception:
        detail = ""
    hints = {
        401: "API Key 无效或已失效，请检查 .env 中的 DEEPSEEK_API_KEY",
        402: "账户余额不足，请先充值",
        429: "触发限流（请求过于频繁），请稍后重试",
        500: "服务端错误，请稍后重试",
        503: "服务暂时不可用（模型过载），请稍后重试",
    }
    hint = hints.get(err.code, "")
    parts = [f"HTTP {err.code}"]
    if hint:
        parts.append(hint)
    if detail:
        parts.append(detail)
    return " | ".join(parts)


def stream_chat(messages: list[dict], on_delta=None, **over) -> tuple[str, dict]:
    """流式对话。返回 (完整文本, usage)。on_delta(str) 每次收到增量时回调。"""
    if not cfg.api_key:
        raise LLMError("未配置 API Key（.env 里的 DEEPSEEK_API_KEY）")
    payload = _messages_payload(messages, True, **over)
    chunks: list[str] = []
    usage: dict = {}
    try:
        resp = _request("/chat/completions", payload)
    except urllib.error.HTTPError as e:
        raise LLMError(_explain_http_error(e)) from e
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise LLMError(f"网络连接失败：{e}") from e

    with resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for choice in obj.get("choices", []):
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    chunks.append(piece)
                    if on_delta:
                        on_delta(piece)
    return "".join(chunks), usage


def complete(messages: list[dict], **over) -> str:
    """非流式补全（用于生成标题、摘要、草稿等短任务）。"""
    if not cfg.api_key:
        raise LLMError("未配置 API Key（.env 里的 DEEPSEEK_API_KEY）")
    payload = _messages_payload(messages, False, **over)
    try:
        resp = _request("/chat/completions", payload)
    except urllib.error.HTTPError as e:
        raise LLMError(_explain_http_error(e)) from e
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise LLMError(f"网络连接失败：{e}") from e
    with resp:
        obj = json.loads(resp.read().decode("utf-8", "replace"))
    try:
        return obj["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        return ""


def health() -> dict:
    """连通性自检：一次极短的非流式调用。"""
    try:
        text = complete([{"role": "user", "content": "ping"}], max_tokens=4, temperature=0)
        return {"ok": True, "model": cfg.get("MODEL"), "reply": (text or "").strip()[:40]}
    except LLMError as e:
        return {"ok": False, "model": cfg.get("MODEL"), "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "model": cfg.get("MODEL"), "error": f"{type(e).__name__}: {e}"}
