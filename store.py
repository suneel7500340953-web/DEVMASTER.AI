"""Conversation history + usage log in SQLite (stdlib)."""
from __future__ import annotations

import sqlite3
import time
import uuid


class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, created REAL);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT, role TEXT, content TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT, ts REAL, provider TEXT,
            model TEXT, purpose TEXT, tin INTEGER, tout INTEGER, cost REAL);
        """)

    def new_conversation(self) -> str:
        cid = uuid.uuid4().hex[:10]
        self.db.execute("INSERT INTO conversations VALUES(?,?)", (cid, time.time()))
        self.db.commit()
        return cid

    def add_message(self, conv, role, content):
        self.db.execute("INSERT INTO messages(conv,role,content,ts) VALUES(?,?,?,?)", (conv, role, content, time.time()))
        self.db.commit()

    def history(self, conv, max_chars=8000) -> list[dict]:
        rows = self.db.execute("SELECT role,content FROM messages WHERE conv=? ORDER BY id DESC", (conv,)).fetchall()
        out, used = [], 0
        for role, content in rows:
            if used + len(content) > max_chars:
                break
            out.append({"role": role, "content": content})
            used += len(content)
        return out[::-1]

    def add_usage(self, conv, provider, model, purpose, tin, tout, cost):
        self.db.execute("INSERT INTO usage(conv,ts,provider,model,purpose,tin,tout,cost) VALUES(?,?,?,?,?,?,?,?)",
                        (conv, time.time(), provider, model, purpose, tin, tout, cost))
        self.db.commit()

    def usage_summary(self, conv) -> dict:
        r = self.db.execute("SELECT COUNT(*),COALESCE(SUM(tin),0),COALESCE(SUM(tout),0),COUNT(cost),COALESCE(SUM(cost),0) "
                            "FROM usage WHERE conv=?", (conv,)).fetchone()
        return {"calls": r[0], "tokens_in": r[1], "tokens_out": r[2],
                "cost_usd": r[4] if r[3] == r[0] and r[0] else None}
