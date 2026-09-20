# -*- coding: utf-8 -*-
"""端到端测试：会话创建 + 流式对话 + 联网兜底 + 草稿回写。

用法：python tools/test_chat.py [问题] [模式]     模式 = auto | kb | web
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8765"


def post(path: str, payload: dict, stream: bool = False):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=300)
    if not stream:
        return json.loads(resp.read().decode("utf-8"))
    return resp


def chat(question: str, mode: str = "auto", session_id: str | None = None, show: int = 400) -> dict:
    resp = post("/api/chat", {"message": question, "mode": mode, "session_id": session_id}, stream=True)
    text = ""
    events: list[str] = []
    meta: dict = {}
    buf = ""
    for raw in resp:
        buf += raw.decode("utf-8", "replace")
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            ev, data = "message", ""
            for line in block.split("\n"):
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data += line[5:].strip()
            if not data:
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            events.append(ev)
            if ev == "delta":
                text += payload.get("t", "")
            elif ev in ("meta", "sources", "done", "web"):
                meta.setdefault(ev, []).append(payload)
            elif ev == "error":
                print("   !! error:", payload)
    answer = text.strip()
    print(f"\n【问题】{question}   （模式={mode}）")
    print(f"  事件流：{' → '.join(dict.fromkeys(events))}")
    m = (meta.get("meta") or [{}])[0]
    print(f"  路由：{m.get('route')}  置信度：{m.get('confidence')}  {m.get('detail')}")
    hits = m.get("hits") or []
    for h in hits[:3]:
        print(f"    · [{h['doc_id']}] {h['title']} › {h['heading']}  (score={h['score']})")
    print(f"  回答（前 {show} 字）：\n{'-' * 70}\n{answer[:show]}\n{'-' * 70}")
    print(f"  回答总长：{len(answer)} 字")
    d = (meta.get("done") or [{}])[0]
    print(f"  usage={d.get('usage')} elapsed={d.get('elapsed')} draft={d.get('draft')}")
    return {"answer": answer, "meta": meta, "session": d.get("session_id")}


def main() -> None:
    q = sys.argv[1] if len(sys.argv) > 1 else "磁盘 inode 满了怎么办"
    mode = sys.argv[2] if len(sys.argv) > 2 else "auto"

    # 1) 建会话
    s = post("/api/sessions", {"title": "端到端测试"})
    sid = s["session"]["id"]
    print(f"会话已创建：{sid}")

    # 2) 对话
    r1 = chat(q, mode, sid)
    sid = r1["session"] or sid

    # 3) 追问（验证多轮上下文）
    if len(sys.argv) <= 2:
        chat("那具体怎么排查？给我三条命令", "auto", sid, show=500)

    # 4) 会话持久化校验
    msgs = json.loads(urllib.request.urlopen(f"{BASE}/api/sessions/{sid}/messages", timeout=20).read())
    print(f"\n会话消息数：{len(msgs.get('messages', []))}（应 ≥4）")
    for m in msgs.get("messages", []):
        print(f"   #{m['id']} {m['role']:<9} {len(m['content']):>5} 字  "
              f"route={(m.get('meta') or {}).get('route')}")
    print()


if __name__ == "__main__":
    main()
