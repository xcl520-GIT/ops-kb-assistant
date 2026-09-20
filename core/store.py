# -*- coding: utf-8 -*-
"""会话持久化（SQLite）。

表结构：
    sessions(id, title, created_at, updated_at, message_count)
    messages(id, session_id, role, content, meta, created_at)

设计：
    - 单连接 + 线程锁（ThreadingHTTPServer 是多线程的）
    - WAL 模式，读写不互斥
    - meta 字段存 JSON：来源引用、检索方式、耗时等
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime

from .config import cfg

_lock = threading.RLock()
_con: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '新对话',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    meta TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    global _con
    if _con is None:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        _con = sqlite3.connect(str(cfg.db_path), check_same_thread=False)
        _con.row_factory = sqlite3.Row
        _con.execute("PRAGMA journal_mode=WAL")
        _con.execute("PRAGMA synchronous=NORMAL")
        _con.executescript(SCHEMA)
        _con.commit()
    return _con


def init() -> None:
    with _lock:
        _conn()


# ---------------------------------------------------------------- 会话

def create_session(title: str = "新对话") -> dict:
    sid = uuid.uuid4().hex[:16]
    ts = now()
    with _lock:
        con = _conn()
        con.execute("INSERT INTO sessions(id,title,created_at,updated_at,message_count) VALUES(?,?,?,?,0)",
                    (sid, title or "新对话", ts, ts))
        con.commit()
    return {"id": sid, "title": title or "新对话", "created_at": ts, "updated_at": ts, "message_count": 0}


def list_sessions(limit: int = 200) -> list[dict]:
    with _lock:
        rows = _conn().execute(
            "SELECT id,title,created_at,updated_at,message_count FROM sessions "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_session(sid: str) -> dict | None:
    with _lock:
        row = _conn().execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return dict(row) if row else None


def rename_session(sid: str, title: str) -> None:
    with _lock:
        con = _conn()
        con.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (title[:60], now(), sid))
        con.commit()


def delete_session(sid: str) -> None:
    with _lock:
        con = _conn()
        con.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        con.execute("DELETE FROM sessions WHERE id=?", (sid,))
        con.commit()


def _touch(sid: str) -> None:
    con = _conn()
    con.execute(
        "UPDATE sessions SET updated_at=?, message_count=(SELECT COUNT(*) FROM messages WHERE session_id=?) WHERE id=?",
        (now(), sid, sid),
    )


# ---------------------------------------------------------------- 消息

def add_message(sid: str, role: str, content: str, meta: dict | None = None) -> int:
    with _lock:
        con = _conn()
        cur = con.execute(
            "INSERT INTO messages(session_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
            (sid, role, content or "", json.dumps(meta or {}, ensure_ascii=False), now()),
        )
        _touch(sid)
        con.commit()
        return int(cur.lastrowid or 0)


def get_messages(sid: str, limit: int = 500) -> list[dict]:
    with _lock:
        rows = _conn().execute(
            "SELECT id,role,content,meta,created_at FROM messages WHERE session_id=? ORDER BY id LIMIT ?",
            (sid, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except json.JSONDecodeError:
            d["meta"] = {}
        out.append(d)
    return out


def history_for_llm(sid: str, turns: int = 6) -> list[dict]:
    """取最近 N 轮对话，转成 OpenAI 消息格式（user/assistant 成对）。"""
    msgs = get_messages(sid)
    convo = [m for m in msgs if m["role"] in ("user", "assistant") and m["content"]]
    # 只保留最近 turns 轮（1 轮 = 2 条）
    tail = convo[-(turns * 2):] if turns > 0 else []
    return [{"role": m["role"], "content": m["content"]} for m in tail]


def search_messages(keyword: str, limit: int = 50) -> list[dict]:
    if not keyword.strip():
        return []
    with _lock:
        rows = _conn().execute(
            "SELECT m.id,m.session_id,m.role,m.content,m.created_at,s.title AS session_title "
            "FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE m.content LIKE ? ORDER BY m.id DESC LIMIT ?",
            (f"%{keyword}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with _lock:
        con = _conn()
        s = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        m = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return {"sessions": s, "messages": m}
