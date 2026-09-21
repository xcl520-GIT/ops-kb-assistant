# -*- coding: utf-8 -*-
r"""诊断 LLM API Key 是否可用。

做两件事：
  1) 读配置文件里的 Key（脱敏打印）
  2) 直连服务端，先 GET /models 探活，再发一条最小 chat 请求实测

用法：
    python tools\check_api_key.py
    python tools\check_api_key.py --env-file "D:\x\.env"
    python tools\check_api_key.py --key-env MY_KEY      # 从环境变量取 Key 测
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_ENV = ROOT / ".env"
KEY_NAME = "DEEPSEEK_API_KEY"
BASE_FALLBACK = "https://api.deepseek.com"


def mask(v: str) -> str:
    if not v:
        return "(空)"
    return v[:6] + "…" + v[-4:] if len(v) > 12 else v[:2] + "…"


def read_env_value(path: pathlib.Path, name: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(name + "="):
            return s.split("=", 1)[1].strip()
    return ""


def call(url: str, key: str, payload: dict | None, timeout: float = 25.0):
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "ops-kb-assistant/1.0")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return r.status, body, (time.time() - t0) * 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=str(DEFAULT_ENV))
    ap.add_argument("--key-env", default="", help="从指定环境变量取 Key，忽略配置文件")
    args = ap.parse_args()

    env_path = pathlib.Path(args.env_file)

    if args.key_env:
        key = os.environ.get(args.key_env, "").strip()
        source = f"环境变量 {args.key_env}"
    else:
        key = read_env_value(env_path, KEY_NAME)
        source = str(env_path)

    base = read_env_value(env_path, "DEEPSEEK_BASE_URL").rstrip("/") or BASE_FALLBACK
    model = read_env_value(env_path, "MODEL") or "deepseek-chat"

    print(f"Key 来源：{source}")
    print(f"Key     ：{mask(key)}   长度 {len(key)}")
    print(f"Base URL：{base}")
    print(f"模型    ：{model}")
    print("-" * 52)

    if not key:
        print("✗ 没读到 Key")
        return 2

    ok = True

    # 1) 探活 + 列模型
    try:
        status, body, ms = call(f"{base}/models", key, None)
        try:
            ids = [m.get("id") for m in json.loads(body).get("data", [])]
        except Exception:
            ids = []
        print(f"✓ GET  /models         HTTP {status}   {ms:.0f} ms")
        if ids:
            print(f"  可用模型：{', '.join(str(i) for i in ids)}")
        if model not in ids and ids:
            print(f"  ⚠️  配置的模型 '{model}' 不在列表里")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        print(f"✗ GET  /models         HTTP {e.code}")
        print(f"  {detail}")
        ok = False
    except Exception as e:
        print(f"✗ GET  /models         {type(e).__name__}: {e}")
        ok = False

    # 2) 最小 chat 实测
    try:
        status, body, ms = call(f"{base}/chat/completions", key, {
            "model": model,
            "messages": [{"role": "user", "content": "回一个字：好"}],
            "max_tokens": 4,
            "stream": False,
        })
        try:
            txt = json.loads(body)["choices"][0]["message"]["content"].strip()
        except Exception:
            txt = body[:200]
        print(f"✓ POST /chat/completions HTTP {status}  {ms:.0f} ms   回复：{txt!r}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        print(f"✗ POST /chat/completions HTTP {e.code}")
        print(f"  {detail}")
        ok = False
    except Exception as e:
        print(f"✗ POST /chat/completions {type(e).__name__}: {e}")
        ok = False

    print("-" * 52)
    print("结论：Key 可用 ✅" if ok else "结论：Key 不可用 ❌（看上面报错）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
