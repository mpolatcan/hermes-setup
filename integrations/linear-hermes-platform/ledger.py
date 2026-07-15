"""Persistent deduplication for Linear webhook deliveries."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class DeliveryLedger:
    """SQLite-backed claim/done ledger compatible with the former bridge."""

    def __init__(
        self,
        path: str,
        *,
        processing_timeout_seconds: int = 300,
        retention_seconds: int = 604800,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.processing_timeout_seconds = processing_timeout_seconds
        self.retention_seconds = retention_seconds
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS deliveries ("
            "webhook_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._db.commit()
        self.prune()

    def claim(self, webhook_id: str, *, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT state, updated_at FROM deliveries WHERE webhook_id = ?",
                (webhook_id,),
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO deliveries(webhook_id, state, updated_at) VALUES (?, 'processing', ?)",
                    (webhook_id, now),
                )
                self._db.commit()
                return True
            state, updated_at = row
            if state == "processing" and now - int(updated_at) > self.processing_timeout_seconds:
                self._db.execute(
                    "UPDATE deliveries SET updated_at = ? WHERE webhook_id = ?",
                    (now, webhook_id),
                )
                self._db.commit()
                return True
            self._db.rollback()
            return False

    def mark_done(self, webhook_id: str, *, now: int | None = None) -> None:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "UPDATE deliveries SET state = 'done', updated_at = ? WHERE webhook_id = ?",
                (now, webhook_id),
            )
            self._db.commit()

    def release(self, webhook_id: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM deliveries WHERE webhook_id = ? AND state = 'processing'",
                (webhook_id,),
            )
            self._db.commit()

    def prune(self, *, now: int | None = None) -> int:
        now = int(time.time()) if now is None else int(now)
        cutoff = now - self.retention_seconds
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM deliveries WHERE state = 'done' AND updated_at < ?",
                (cutoff,),
            )
            self._db.commit()
            return int(cur.rowcount)

    def close(self) -> None:
        with self._lock:
            self._db.close()
