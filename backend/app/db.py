from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid4().hex


class Database:
    """Small local store. Each operation owns its connection and a short transaction."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(kind, id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_stream ON events(stream, seq);
            """)

    @contextmanager
    def connect(self):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=10)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def put(self, kind: str, data: dict) -> dict:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO records VALUES(?, ?, ?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (kind, data["id"], json.dumps(data, ensure_ascii=False)),
            )
        return data

    def get(self, kind: str, record_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
        if row is None:
            raise KeyError(f"{kind} not found")
        return json.loads(row[0])

    def list(self, kind: str, **filters) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
        items = [json.loads(row[0]) for row in rows]
        return [item for item in items if all(item.get(k) == v for k, v in filters.items())]

    def event(self, stream: str, event_type: str, **data) -> dict:
        event = {"type": event_type, "time": now(), **data}
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO events(stream,data) VALUES(?,?)", (stream, json.dumps(event, ensure_ascii=False))
            )
            event["seq"] = cursor.lastrowid
        return event

    def events(self, stream: str, after: int = 0) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT seq,data FROM events WHERE stream=? AND seq>? ORDER BY seq LIMIT 200", (stream, after)
            ).fetchall()
        return [{**json.loads(row[1]), "seq": row[0]} for row in rows]
