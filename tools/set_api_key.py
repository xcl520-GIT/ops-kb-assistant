# -*- coding: utf-8 -*-
r"""改写 .env 里的 DEEPSEEK_API_KEY。

密钥通过环境变量传入，不落到命令行参数里（避免进入 shell 历史与进程列表）。

用法：
    set NEW_KEY=sk-xxxxxxxx
    python tools\set_api_key.py

只查看当前值（脱敏）：
    python tools\set_api_key.py --show

指定别的配置文件：
    python tools\set_api_key.py --env-file "D:\somewhere\.env"
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_ENV = ROOT / ".env"
KEY_NAME = "DEEPSEEK_API_KEY"


def mask(value: str) -> str:
    if not value:
        return "(空)"
    if len(value) <= 12:
        return value[:2] + "…"
    return f"{value[:6]}…{value[-4:]}  (长度 {len(value)})"


def read_key(path: pathlib.Path) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(KEY_NAME + "="):
            return s.split("=", 1)[1].strip()
    return ""


def write_key(path: pathlib.Path, value: str) -> tuple[str, bool]:
    """返回 (旧值, 是否新增了行)。"""
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    old = ""
    hit = False
    out = []
    for line in lines:
        if line.strip().startswith(KEY_NAME + "="):
            old = line.split("=", 1)[1].strip()
            out.append(f"{KEY_NAME}={value}")
            hit = True
        else:
            out.append(line)
    if not hit:
        out.append(f"{KEY_NAME}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return old, not hit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=str(DEFAULT_ENV))
    ap.add_argument("--show", action="store_true", help="只看当前值，不改")
    args = ap.parse_args()

    path = pathlib.Path(args.env_file)

    if args.show:
        print(f"配置文件：{path}  存在={path.exists()}")
        print(f"{KEY_NAME} = {mask(read_key(path))}")
        return 0

    new = os.environ.get("NEW_KEY", "").strip()
    if not new:
        print("环境变量 NEW_KEY 为空。用法：set NEW_KEY=sk-xxxx 后再执行本脚本。",
              file=sys.stderr)
        return 2

    # 基本形态校验：不拦非 sk- 开头的兼容服务，只做提醒
    warn = []
    if len(new) < 20:
        warn.append("长度偏短（<20），疑似不完整")
    if not new.startswith("sk-"):
        warn.append("不是 sk- 开头，确认不是填成了别的服务的 Key")

    old, appended = write_key(path, new)

    print(f"配置文件：{path}")
    print(f"  旧值：{mask(old)}")
    print(f"  新值：{mask(new)}")
    print(f"  方式：{'追加新行' if appended else '替换原行'}")
    for w in warn:
        print(f"  ⚠️  {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
