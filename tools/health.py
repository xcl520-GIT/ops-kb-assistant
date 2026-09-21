# -*- coding: utf-8 -*-
r"""体检脚本：端口占用 / 服务可达性 / 知识库索引 / 会话数。

用法：
    python tools\health.py
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


def env(name: str, default: str = "") -> str:
    p = ROOT / ".env"
    if not p.exists():
        return default
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(name + "="):
            return s.split("=", 1)[1].strip()
    return default


def listeners(port: str) -> list[str]:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=15, errors="replace").stdout
    except Exception as e:  # noqa: BLE001
        print(f"! netstat 调用失败：{e}")
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1].endswith(":" + port) and parts[3].upper() == "LISTENING":
            rows.append(line.strip())
    return rows


def get_json(url: str, timeout: float = 20.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    host = env("HOST", "127.0.0.1") or "127.0.0.1"
    port = env("PORT", "8765") or "8765"
    base = f"http://{host}:{port}"

    print(f"项目目录：{ROOT}")
    print(f"服务地址：{base}")
    print(f"知识库  ：{env('KB_DIR', './kb')}")
    print("-" * 58)

    rows = listeners(port)
    if not rows:
        print(f"✗ 端口 {port} 没有监听 —— 服务未启动")
        print("  双击 start.bat 启动后，再跑一次体检")
        return 1
    print(f"✓ 端口 {port} 正在监听（{len(rows)} 条记录）")
    for r in rows:
        print("    " + r)
    if len(rows) > 1:
        print("  ⚠️  出现多条监听记录：可能存在双实例，先跑 stop.bat 再重新启动")
    print()

    try:
        h = get_json(base + "/api/health")
    except urllib.error.URLError as e:
        print(f"✗ 服务无响应：{e}")
        print("  端口很可能被别的程序占用；停止后改 .env 里的 PORT 再试")
        return 1

    kb = h.get("kb") or {}
    chat = h.get("chat") or {}
    llm = h.get("llm") or {}

    print("✓ /api/health 正常")
    print(f"    知识库：{kb.get('docs')} 篇 / {kb.get('chunks')} 块 / "
          f"{kb.get('chars', 0):,} 字    索引过期={kb.get('stale')}")
    print(f"    知识库目录存在={kb.get('kb_dir_exists')}    {kb.get('kb_dir')}")
    print(f"    会话：{chat.get('sessions')} 个 / 消息 {chat.get('messages')} 条")
    print(f"    模型：{llm.get('model')}    Key 已配置={llm.get('api_key_set')}")
    print()
    print("下一步：check_api_key.py 实测 Key 能不能真正调通")
    return 0


if __name__ == "__main__":
    sys.exit(main())
