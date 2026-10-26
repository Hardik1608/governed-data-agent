"""Append-only audit log: who asked what, which SQL ran, what was approved, what was answered.

SQLite (WAL) so it survives crashes and needs no server. Every event has a deterministic id
(thread, tool call, kind), so a LangGraph node that re-runs after an interrupt, or a retried
request, records an event once (INSERT OR IGNORE). Rows are never updated or deleted by the
application.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    event_id   TEXT PRIMARY KEY,
    ts         TEXT NOT NULL,
    thread_id  TEXT,
    user_id    TEXT,
    role       TEXT,
    kind       TEXT NOT NULL,
    tool       TEXT,
    sql        TEXT,
    status     TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS audit_thread ON audit_events(thread_id);
CREATE INDEX IF NOT EXISTS audit_ts ON audit_events(ts);
"""

KINDS = ("question", "sql", "approval", "tool", "answer", "error")


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def record(
        self,
        event_id: str,
        kind: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
        role: str | None = None,
        tool: str | None = None,
        sql: str | None = None,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        """Insert one event. Returns False when the event id was already recorded."""
        if kind not in KINDS:
            raise ValueError(f"unknown audit kind {kind}")
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO audit_events "
                "(event_id, ts, thread_id, user_id, role, kind, tool, sql, status, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    datetime.now(UTC).isoformat(timespec="milliseconds"),
                    thread_id,
                    user_id,
                    role,
                    kind,
                    tool,
                    sql,
                    status,
                    json.dumps(detail, default=str) if detail is not None else None,
                ),
            )
            return cur.rowcount == 1

    def events(self, thread_id: str | None = None, limit: int = 500) -> list[dict]:
        query = "SELECT * FROM audit_events"
        args: tuple = ()
        if thread_id is not None:
            query += " WHERE thread_id = ?"
            args = (thread_id,)
        query += " ORDER BY ts, rowid LIMIT ?"
        with self._lock:
            cur = self._conn.execute(query, (*args, limit))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        for row in rows:
            if row.get("detail"):
                row["detail"] = json.loads(row["detail"])
        return rows

    def close(self) -> None:
        with self._lock:
            self._conn.close()
