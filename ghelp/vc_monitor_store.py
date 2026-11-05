"""Persistent SQLite store for Voice Chat Monitor events, settings, and health."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class VCMonitorStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            data_dir = Path("data")
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(data_dir / "vc_monitor.db")
        else:
            self.db_path = str(db_path)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS vc_chats (
                    chat_id INTEGER PRIMARY KEY,
                    enabled INTEGER DEFAULT 1,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS vc_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    name TEXT,
                    username TEXT,
                    kind TEXT,
                    joined_at REAL,
                    claimed_at REAL DEFAULT NULL,
                    acked INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS vc_deletes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    message_id INTEGER,
                    due_at REAL,
                    claimed_at REAL DEFAULT NULL,
                    finished INTEGER DEFAULT 0,
                    success INTEGER DEFAULT 0,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS vc_health (
                    chat_id INTEGER PRIMARY KEY,
                    status TEXT,
                    assistant_id INTEGER,
                    last_seen REAL,
                    detail TEXT,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_vc_events_unacked ON vc_events(acked, claimed_at);
                CREATE INDEX IF NOT EXISTS idx_vc_deletes_pending ON vc_deletes(finished, due_at, claimed_at);
            """)

    def remember_chat(self, chat_id: int) -> None:
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO vc_chats (chat_id, enabled, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (int(chat_id), now),
            )

    def is_enabled(self, chat_id: int) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT enabled FROM vc_chats WHERE chat_id = ?",
                (int(chat_id),),
            )
            row = cur.fetchone()
            if row is None:
                return True
            return bool(row["enabled"])

    def set_enabled(self, chat_id: int, enabled: bool) -> None:
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO vc_chats (chat_id, enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at
                """,
                (int(chat_id), 1 if enabled else 0, now),
            )

    def get_setting(self, chat_id: int) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT enabled, updated_at FROM vc_chats WHERE chat_id = ?",
                (int(chat_id),),
            )
            row = cur.fetchone()
            if row is None:
                return {"enabled": True, "updated_at": None}
            return {"enabled": bool(row["enabled"]), "updated_at": row["updated_at"]}

    def known_chats(self, limit: int = 200) -> List[int]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT chat_id FROM vc_chats ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
            return [int(row["chat_id"]) for row in cur.fetchall()]

    def record_event(
        self,
        chat_id: int,
        user_id: int,
        name: str,
        username: str | None = None,
        kind: str = "user",
        joined_at: float | None = None,
    ) -> int:
        now = joined_at or time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO vc_events (chat_id, user_id, name, username, kind, joined_at, acked)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (int(chat_id), int(user_id), str(name), str(username or ""), str(kind), now),
            )
            return int(cur.lastrowid)

    def claim_events(self, now: float | None = None, limit: int = 25) -> List[Dict[str, Any]]:
        current = now or time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT id, chat_id, user_id, name, username, kind, joined_at
                FROM vc_events
                WHERE acked = 0 AND (claimed_at IS NULL OR claimed_at < ?)
                ORDER BY id ASC
                LIMIT ?
                """,
                (current - 30.0, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE vc_events SET claimed_at = ? WHERE id IN ({placeholders})",
                    [current, *ids],
                )
            return rows

    def ack_event(self, event_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE vc_events SET acked = 1 WHERE id = ?", (int(event_id),))

    def release_event(self, event_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE vc_events SET claimed_at = NULL WHERE id = ?", (int(event_id),))

    def add_pending_delete(
        self,
        chat_id: int,
        message_id: int,
        due_at: float,
        now: float | None = None,
    ) -> int:
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO vc_deletes (chat_id, message_id, due_at, finished)
                VALUES (?, ?, ?, 0)
                """,
                (int(chat_id), int(message_id), float(due_at)),
            )
            return int(cur.lastrowid)

    def claim_pending_deletes(self, now: float | None = None, limit: int = 25) -> List[Dict[str, Any]]:
        current = now or time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT id, chat_id, message_id, due_at
                FROM vc_deletes
                WHERE finished = 0 AND due_at <= ? AND (claimed_at IS NULL OR claimed_at < ?)
                ORDER BY due_at ASC
                LIMIT ?
                """,
                (current, current - 30.0, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE vc_deletes SET claimed_at = ? WHERE id IN ({placeholders})",
                    [current, *ids],
                )
            return rows

    def claim_pending_delete(self, delete_id: int, now: float | None = None) -> Optional[Dict[str, Any]]:
        current = now or time.time()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT id, chat_id, message_id, due_at
                FROM vc_deletes
                WHERE id = ? AND finished = 0 AND (claimed_at IS NULL OR claimed_at < ?)
                """,
                (int(delete_id), current - 30.0),
            )
            row = cur.fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE vc_deletes SET claimed_at = ? WHERE id = ?",
                    (current, int(delete_id)),
                )
                return dict(row)
            return None

    def release_pending_delete(self, delete_id: int) -> None:
        with self._get_connection() as conn:
            conn.execute("UPDATE vc_deletes SET claimed_at = NULL WHERE id = ?", (int(delete_id),))

    def finish_delete(
        self,
        delete_id: int,
        success: bool = True,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE vc_deletes
                SET finished = 1, success = ?, error = ?
                WHERE id = ?
                """,
                (1 if success else 0, str(error or ""), int(delete_id)),
            )

    def record_health(
        self,
        chat_id: int,
        status: str,
        assistant_id: int | None = None,
        last_seen: float | None = None,
        detail: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = last_seen or time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO vc_health (chat_id, status, assistant_id, last_seen, detail, last_error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    status = excluded.status,
                    assistant_id = excluded.assistant_id,
                    last_seen = excluded.last_seen,
                    detail = excluded.detail,
                    last_error = excluded.last_error
                """,
                (
                    int(chat_id),
                    str(status),
                    int(assistant_id) if assistant_id else None,
                    now,
                    str(detail or ""),
                    str(last_error or ""),
                ),
            )

    def get_health(self, chat_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT chat_id, status, assistant_id, last_seen, detail, last_error FROM vc_health WHERE chat_id = ?",
                (int(chat_id),),
            )
            row = cur.fetchone()
            if row is not None:
                return dict(row)
            return None

    def prune(self, now: float | None = None) -> None:
        current = now or time.time()
        cutoff = current - 3600.0
        with self._get_connection() as conn:
            conn.execute("DELETE FROM vc_events WHERE acked = 1 AND joined_at < ?", (cutoff,))
            conn.execute("DELETE FROM vc_deletes WHERE finished = 1 AND due_at < ?", (cutoff,))


_store_instance: VCMonitorStore | None = None


def get_store(db_path: str | Path | None = None) -> VCMonitorStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = VCMonitorStore(db_path=db_path)
    return _store_instance
