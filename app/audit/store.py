from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  telegram_user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  message_id INTEGER,
  kind TEXT NOT NULL,
  raw_input TEXT,
  transcription TEXT,
  llm_model TEXT,
  llm_context TEXT,
  llm_response TEXT,
  interpretation TEXT,
  candidate_scores TEXT,
  validation_result TEXT,
  decision TEXT,
  clarification_state TEXT,
  command TEXT,
  executed INTEGER NOT NULL DEFAULT 0,
  notion_page_id TEXT,
  error TEXT,
  duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
  chat_id INTEGER PRIMARY KEY,
  payload TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY,
  event_id INTEGER REFERENCES events(id),
  chat_id INTEGER NOT NULL,
  reply_message_id INTEGER,
  undo TEXT NOT NULL,
  undone INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT NOT NULL
);
"""

EVENT_COLUMNS = frozenset(
    {"message_id", "kind", "raw_input", "transcription", "llm_model", "llm_context",
     "llm_response", "interpretation", "candidate_scores", "validation_result", "decision",
     "clarification_state", "command", "executed", "notion_page_id", "error", "duration_ms",
     "telegram_user_id", "chat_id"}
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class AuditStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # events
    def new_event(self, *, telegram_user_id: int, chat_id: int, kind: str, **cols) -> int:
        with self._lock:
            cols.update(telegram_user_id=telegram_user_id, chat_id=chat_id, kind=kind)
            self._check_cols(cols)
            cols["ts"] = _iso(datetime.now(UTC))
            keys = ", ".join(cols)
            marks = ", ".join("?" for _ in cols)
            cur = self._conn.execute(f"INSERT INTO events ({keys}) VALUES ({marks})",
                                     list(cols.values()))
            self._conn.commit()
            return int(cur.lastrowid)

    def update_event(self, event_id: int, **cols) -> None:
        with self._lock:
            self._check_cols(cols)
            if not cols:
                return
            sets = ", ".join(f"{k} = ?" for k in cols)
            self._conn.execute(f"UPDATE events SET {sets} WHERE id = ?",
                               [*cols.values(), event_id])
            self._conn.commit()

    def get_event(self, event_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?",
                                     (event_id,)).fetchone()
            return dict(row) if row else None

    # sessions
    def save_session(self, chat_id: int, payload: str, expires_at: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (chat_id, payload, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET payload = excluded.payload, "
                "expires_at = excluded.expires_at",
                (chat_id, payload, _iso(expires_at)),
            )
            self._conn.commit()

    def get_session(self, chat_id: int, now: datetime) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, expires_at FROM sessions WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= _iso(now):
                self.delete_session(chat_id)
                return None
            return row["payload"]

    def delete_session(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    # executions
    def add_execution(
        self, event_id: int, chat_id: int, reply_message_id: int | None, undo: str,
        expires_at: datetime,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO executions (event_id, chat_id, reply_message_id, undo, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_id, chat_id, reply_message_id, undo, _iso(expires_at)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_execution(self, execution_id: int, now: datetime) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE id = ? AND expires_at > ?",
                (execution_id, _iso(now)),
            ).fetchone()
            return dict(row) if row else None

    def latest_execution(self, chat_id: int, now: datetime) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE chat_id = ? AND undone = 0 AND expires_at > ? "
                "ORDER BY id DESC LIMIT 1",
                (chat_id, _iso(now)),
            ).fetchone()
            return dict(row) if row else None

    def mark_undone(self, execution_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE executions SET undone = 1 WHERE id = ?", (execution_id,))
            self._conn.commit()

    @staticmethod
    def _check_cols(cols: dict) -> None:
        bad = set(cols) - EVENT_COLUMNS
        if bad:
            raise ValueError(f"unknown event columns: {sorted(bad)}")
