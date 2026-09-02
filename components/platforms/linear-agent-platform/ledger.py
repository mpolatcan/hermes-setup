"""Persistent deduplication and writeback outbox for Linear."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OutboxItem:
    id: str
    aggregate_key: str
    sequence: int
    operation: str
    payload: dict[str, Any]
    attempts: int


class DeliveryLedger:
    """SQLite-backed inbound ledger and ordered writeback outbox."""

    def __init__(
        self,
        path: str,
        *,
        processing_timeout_seconds: int = 300,
        retention_seconds: int = 604800,
        outbox_claim_timeout_seconds: int = 60,
        startup_recovery: bool = True,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        parent_stat = self.path.parent.stat()
        if hasattr(os, "geteuid") and parent_stat.st_uid != os.geteuid():
            raise RuntimeError("Linear ledger directory must be owned by the profile user")
        if stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise RuntimeError("Linear ledger directory must be owner-only (0700)")
        self.processing_timeout_seconds = processing_timeout_seconds
        self.retention_seconds = retention_seconds
        self.outbox_claim_timeout_seconds = outbox_claim_timeout_seconds
        self._lock = threading.Lock()
        sidecars = (Path(f"{self.path}-wal"), Path(f"{self.path}-shm"))
        for sidecar in sidecars:
            if sidecar.is_symlink():
                raise RuntimeError("Linear ledger SQLite sidecar must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            opened = os.fstat(fd)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            actual = os.lstat(self.path)
            if stat.S_ISLNK(actual.st_mode) or (
                actual.st_dev, actual.st_ino
            ) != (opened.st_dev, opened.st_ino):
                self._db.close()
                raise RuntimeError("Linear ledger changed while it was being opened")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
        finally:
            os.close(fd)
        for sidecar in sidecars:
            if sidecar.is_symlink():
                self._db.close()
                raise RuntimeError("Linear ledger SQLite sidecar must not be a symlink")

        self._db.execute(
            "CREATE TABLE IF NOT EXISTS deliveries ("
            "webhook_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbox ("
            "id TEXT PRIMARY KEY, "
            "aggregate_key TEXT NOT NULL, "
            "sequence INTEGER NOT NULL, "
            "operation TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK(state IN ('pending', 'in_flight', 'delivered', 'dead')), "
            "attempts INTEGER NOT NULL DEFAULT 0, "
            "next_attempt_at REAL NOT NULL, "
            "last_error TEXT, "
            "created_at INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL, "
            "delivered_at INTEGER, "
            "UNIQUE(aggregate_key, sequence))"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS outbox_due_idx "
            "ON outbox(state, next_attempt_at, created_at)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS waiting_executions ("
            "session_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, delivery_key TEXT NOT NULL, "
            "prompt_json TEXT NOT NULL, blockers_json TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK(state IN "
            "('waiting', 'resuming', 'resumed', 'canceled', 'failed')), "
            "revision INTEGER NOT NULL DEFAULT 1, last_error TEXT, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, resumed_at INTEGER)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS waiting_issue_state_idx "
            "ON waiting_executions(issue_id, state, updated_at)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS activation_waits ("
            "issue_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE, "
            "delivery_key TEXT NOT NULL, prompt_json TEXT NOT NULL, activation_key TEXT UNIQUE, "
            "state TEXT NOT NULL CHECK(state IN "
            "('waiting', 'dispatch_unknown', 'resumed', 'canceled', 'failed')), "
            "last_error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "resumed_at INTEGER)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS activation_state_idx "
            "ON activation_waits(state, updated_at)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS direct_activation_grants ("
            "operation_key TEXT PRIMARY KEY, issue_id TEXT UNIQUE, "
            "source_platform TEXT NOT NULL, source_user_id TEXT NOT NULL, "
            "source_message_id TEXT NOT NULL, source_session_id TEXT NOT NULL, "
            "source_profile TEXT NOT NULL, policy_result TEXT NOT NULL, "
            "actor_id TEXT NOT NULL, team_id TEXT NOT NULL, issue_fingerprint TEXT NOT NULL, "
            "session_id TEXT NOT NULL DEFAULT '', activation_key TEXT NOT NULL DEFAULT '', "
            "state TEXT NOT NULL CHECK(state IN "
            "('reserved', 'granted', 'claimed', 'dispatch_unknown', 'dispatched', "
            "'canceled', 'failed')), "
            "last_error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS direct_activation_state_idx "
            "ON direct_activation_grants(state, updated_at)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS direct_activation_events ("
            "issue_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE, "
            "delivery_key TEXT NOT NULL, prompt_json TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK(state IN "
            "('waiting', 'claimed', 'dispatch_unknown', 'dispatched', 'canceled', 'failed')), "
            "last_error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS direct_activation_event_state_idx "
            "ON direct_activation_events(state, updated_at)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS manager_activations ("
            "issue_id TEXT PRIMARY KEY, activation_key TEXT NOT NULL UNIQUE, "
            "state TEXT NOT NULL CHECK(state IN "
            "('claimed', 'delegation_unknown', 'delegated', 'dispatch_unknown', "
            "'session_started', 'canceled', 'failed')), "
            "session_id TEXT, evidence_json TEXT NOT NULL, last_error TEXT, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS issue_session_bindings ("
            "issue_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS channel_routes ("
            "operation_key TEXT PRIMARY KEY, source_platform TEXT NOT NULL, "
            "source_chat_id TEXT NOT NULL, source_thread_id TEXT NOT NULL, "
            "source_message_id TEXT NOT NULL, source_user_id TEXT NOT NULL, "
            "source_user_name TEXT NOT NULL, source_chat_type TEXT NOT NULL, "
            "source_profile TEXT NOT NULL, source_scope_id TEXT NOT NULL, "
            "source_via_relay INTEGER NOT NULL, "
            "issue_ref TEXT NOT NULL, "
            "command_text TEXT NOT NULL, issue_id TEXT NOT NULL DEFAULT '', "
            "session_id TEXT NOT NULL DEFAULT '', "
            "state TEXT NOT NULL CHECK(state IN "
            "('claimed', 'dispatching', 'dispatched', 'blocked', 'failed', 'ambiguous')), "
            "last_error TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, "
            "next_attempt_at REAL NOT NULL DEFAULT 0, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS channel_routes_issue_state_idx "
            "ON channel_routes(issue_id, state, updated_at)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS closure_reconciliations ("
            "closure_key TEXT PRIMARY KEY, issue_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "outbox_id TEXT NOT NULL UNIQUE, evidence_json TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK(state IN ('pending', 'completed', 'failed')), "
            "last_error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "completed_at INTEGER)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS closure_issue_state_idx "
            "ON closure_reconciliations(issue_id, state, updated_at)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pending_closure_events ("
            "issue_id TEXT PRIMARY KEY, event_revision REAL NOT NULL, "
            "event_json TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS progress_turns ("
            "aggregate_key TEXT PRIMARY KEY, turn_key TEXT NOT NULL, "
            "fenced INTEGER NOT NULL CHECK(fenced IN (0, 1)), updated_at INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS turn_decisions ("
            "decision_id TEXT PRIMARY KEY, "
            "agent_session_id TEXT NOT NULL, issue_id TEXT NOT NULL, "
            "hermes_session_id TEXT NOT NULL, goal_generation INTEGER NOT NULL, "
            "ordinal INTEGER NOT NULL, "
            "outcome TEXT NOT NULL CHECK(outcome IN "
            "('success', 'continue', 'awaiting_input', 'approval', 'blocked', 'stopped')), "
            "dispatch_state TEXT NOT NULL CHECK(dispatch_state IN "
            "('pending', 'enqueued', 'running', 'completed', 'fenced')), "
            "error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
            "completed_at INTEGER, "
            "UNIQUE(agent_session_id, goal_generation, ordinal))"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS turn_decisions_recovery_idx "
            "ON turn_decisions(dispatch_state, created_at)"
        )
        if int(self._db.execute("PRAGMA user_version").fetchone()[0]) < 9:
            self._db.execute("PRAGMA user_version=9")
        # A process restart proves that no previous local worker still owns a
        # resuming claim. Outbound-only clients may open this database while
        # the gateway is live, so they must not run process-start recovery.
        if startup_recovery:
            self._db.execute(
                "UPDATE waiting_executions SET state = 'waiting', "
                "last_error = COALESCE(last_error, 'Recovered interrupted resume'), "
                "updated_at = ? WHERE state = 'resuming'",
                (int(time.time()),),
            )
            self._db.execute(
                "UPDATE manager_activations SET state='failed', "
                "last_error=COALESCE(last_error, 'Recovered interrupted manager delegation'), "
                "updated_at=? WHERE state='claimed'",
                (int(time.time()),),
            )
            self._db.execute(
                "UPDATE channel_routes SET state='ambiguous', "
                "last_error='restart_during_dispatch', updated_at=? WHERE state='dispatching'",
                (int(time.time()),),
            )
            self._db.execute(
                "UPDATE closure_reconciliations SET state = 'completed', last_error = NULL, "
                "updated_at = COALESCE((SELECT delivered_at FROM outbox "
                "WHERE outbox.id = closure_reconciliations.outbox_id), updated_at), "
                "completed_at = COALESCE((SELECT delivered_at FROM outbox "
                "WHERE outbox.id = closure_reconciliations.outbox_id), completed_at) "
                "WHERE state != 'completed' AND EXISTS (SELECT 1 FROM outbox "
                "WHERE outbox.id = closure_reconciliations.outbox_id "
                "AND outbox.state = 'delivered')"
            )
        self._db.commit()
        self._secure_state_files()
        if startup_recovery:
            self.prune()

    def _secure_state_files(self) -> None:
        """Keep the database and SQLite sidecars private to the profile owner."""
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not candidate.exists():
                continue
            if candidate.is_symlink():
                raise RuntimeError("Linear ledger state path must not be a symlink")
            candidate.chmod(0o600)

    def bind_issue_session(
        self, issue_id: str, session_id: str, *, now: int | None = None,
    ) -> None:
        """Record the latest locally accepted Agent Session creation for an issue."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "INSERT INTO issue_session_bindings(issue_id, session_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(issue_id) DO UPDATE SET "
                "session_id=excluded.session_id, updated_at=excluded.updated_at",
                (issue_id, session_id, now, now),
            )
            self._db.commit()

    def get_issue_session(self, issue_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT session_id FROM issue_session_bindings WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def claim_channel_route(
        self,
        operation_key: str,
        *,
        source_platform: str,
        source_chat_id: str,
        source_thread_id: str,
        source_message_id: str = "",
        source_user_id: str = "",
        source_user_name: str = "",
        source_chat_type: str = "dm",
        source_profile: str = "",
        source_scope_id: str = "",
        source_via_relay: bool = False,
        issue_ref: str = "",
        command_text: str = "",
        issue_id: str = "",
        session_id: str = "",
        now: int | None = None,
    ) -> bool:
        """Durably reserve a source command before any remote authorization lookup."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO channel_routes("
                "operation_key, source_platform, source_chat_id, source_thread_id, "
                "source_message_id, source_user_id, source_user_name, issue_ref, command_text, "
                "source_chat_type, source_profile, source_scope_id, source_via_relay, "
                "state, next_attempt_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?)",
                (
                    operation_key,
                    source_platform,
                    source_chat_id,
                    source_thread_id,
                    source_message_id,
                    source_user_id,
                    source_user_name,
                    issue_ref or issue_id,
                    command_text,
                    source_chat_type,
                    source_profile,
                    source_scope_id,
                    int(source_via_relay),
                    float(now),
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 1 and (issue_id or session_id):
                self._db.execute(
                    "UPDATE channel_routes SET issue_id=?, session_id=? WHERE operation_key=?",
                    (issue_id, session_id, operation_key),
                )
            self._db.commit()
        return cursor.rowcount == 1

    def claim_due_channel_routes(
        self, *, limit: int, now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Claim a bounded batch for the adapter's single recovery worker."""
        if limit <= 0:
            return []
        now = time.time() if now is None else float(now)
        with self._lock:
            rows = self._db.execute(
                "SELECT operation_key FROM channel_routes WHERE state='claimed' "
                "AND next_attempt_at <= ? ORDER BY created_at, operation_key LIMIT ?",
                (now, int(limit)),
            ).fetchall()
            keys = [str(row[0]) for row in rows]
            if keys:
                placeholders = ",".join("?" for _ in keys)
                self._db.execute(
                    f"UPDATE channel_routes SET attempt_count=attempt_count+1, updated_at=? "
                    f"WHERE state='claimed' AND operation_key IN ({placeholders})",
                    (int(now), *keys),
                )
                self._db.commit()
        return [route for key in keys if (route := self.get_channel_route(key)) is not None]

    def set_channel_route_target(
        self, operation_key: str, issue_id: str, session_id: str, *, now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cursor = self._db.execute(
                "UPDATE channel_routes SET issue_id=?, session_id=?, updated_at=? "
                "WHERE operation_key=? AND state='claimed'",
                (issue_id, session_id, now, operation_key),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def retry_channel_route(
        self,
        operation_key: str,
        *,
        error: str,
        next_attempt_at: float,
        max_attempts: int,
        now: int | None = None,
    ) -> bool:
        """Back off a pre-dispatch failure, terminally failing at the fixed attempt bound."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cursor = self._db.execute(
                "UPDATE channel_routes SET "
                "state=CASE WHEN attempt_count >= ? THEN 'failed' ELSE 'claimed' END, "
                "last_error=?, next_attempt_at=?, updated_at=? "
                "WHERE operation_key=? AND state='claimed'",
                (int(max_attempts), error, float(next_attempt_at), now, operation_key),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def mark_channel_route(
        self,
        operation_key: str,
        state: str,
        *,
        error: str | None = None,
        now: int | None = None,
    ) -> bool:
        transitions = {
            "dispatching": "claimed",
            "dispatched": "dispatching",
            "blocked": "claimed",
            "failed": "claimed",
            "ambiguous": "dispatching",
        }
        if state not in transitions:
            raise ValueError(f"Unsupported channel route state: {state}")
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cursor = self._db.execute(
                "UPDATE channel_routes SET state=?, last_error=?, updated_at=? "
                "WHERE operation_key=? AND state=?",
                (state, error, now, operation_key, transitions[state]),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def get_channel_route(self, operation_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT operation_key, source_platform, source_chat_id, source_thread_id, "
                "source_message_id, source_user_id, source_user_name, issue_ref, command_text, "
                "source_chat_type, source_profile, source_scope_id, source_via_relay, "
                "issue_id, session_id, state, last_error, attempt_count, next_attempt_at, "
                "created_at, updated_at FROM channel_routes "
                "WHERE operation_key=?",
                (operation_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "operation_key": str(row[0]),
            "source_platform": str(row[1]),
            "source_chat_id": str(row[2]),
            "source_thread_id": str(row[3]),
            "source_message_id": str(row[4]),
            "source_user_id": str(row[5]),
            "source_user_name": str(row[6]),
            "issue_ref": str(row[7]),
            "command_text": str(row[8]),
            "source_chat_type": str(row[9]),
            "source_profile": str(row[10]),
            "source_scope_id": str(row[11]),
            "source_via_relay": bool(row[12]),
            "issue_id": str(row[13]),
            "session_id": str(row[14]),
            "state": str(row[15]),
            "last_error": row[16],
            "attempt_count": int(row[17]),
            "next_attempt_at": float(row[18]),
            "created_at": int(row[19]),
            "updated_at": int(row[20]),
        }

    @staticmethod
    def _decode_activation_wait(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "issue_id": str(row[0]),
            "session_id": str(row[1]),
            "delivery_key": str(row[2]),
            "prompt": json.loads(row[3]),
            "activation_key": str(row[4]) if row[4] is not None else None,
            "state": str(row[5]),
            "last_error": row[6],
            "created_at": int(row[7]),
            "updated_at": int(row[8]),
            "resumed_at": int(row[9]) if row[9] is not None else None,
        }

    def put_activation_wait(
        self,
        session_id: str,
        issue_id: str,
        delivery_key: str,
        prompt: dict[str, Any],
        *,
        now: int | None = None,
    ) -> None:
        """Persist a parked Planned session before acknowledging its creation."""
        now = int(time.time()) if now is None else int(now)
        prompt_json = json.dumps(
            prompt, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO activation_waits("
                "issue_id, session_id, delivery_key, prompt_json, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'waiting', ?, ?) "
                "ON CONFLICT(issue_id) DO UPDATE SET "
                "session_id=excluded.session_id, delivery_key=excluded.delivery_key, "
                "prompt_json=excluded.prompt_json, updated_at=excluded.updated_at "
                "WHERE activation_waits.state = 'waiting'",
                (issue_id, session_id, delivery_key, prompt_json, now, now),
            )
            self._db.commit()

    def get_activation_wait(self, issue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT issue_id, session_id, delivery_key, prompt_json, activation_key, "
                "state, last_error, created_at, updated_at, resumed_at "
                "FROM activation_waits WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
        return self._decode_activation_wait(row) if row else None

    def claim_activation(
        self,
        issue_id: str,
        activation_key: str,
        *,
        now: int | None = None,
    ) -> bool:
        """Fence one activation dispatch; an interrupted call remains ambiguous."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT state, activation_key FROM activation_waits WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
            if row is None or str(row[0]) != "waiting":
                self._db.rollback()
                return False
            existing_key = str(row[1]) if row[1] is not None else None
            if existing_key not in (None, activation_key):
                self._db.rollback()
                return False
            self._db.execute(
                "UPDATE activation_waits SET state = 'dispatch_unknown', activation_key = ?, "
                "last_error = NULL, updated_at = ? WHERE issue_id = ? AND state = 'waiting'",
                (activation_key, now, issue_id),
            )
            self._db.commit()
            return True

    def mark_activation_resumed(
        self, issue_id: str, *, now: int | None = None,
    ) -> None:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "UPDATE activation_waits SET state = 'resumed', updated_at = ?, resumed_at = ? "
                "WHERE issue_id = ? AND state = 'dispatch_unknown'",
                (now, now, issue_id),
            )
            self._db.commit()

    def fail_activation(
        self, issue_id: str, error: str, *, now: int | None = None,
    ) -> None:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "UPDATE activation_waits SET last_error = ?, updated_at = ? "
                "WHERE issue_id = ? AND state = 'dispatch_unknown'",
                (error[:1000], now, issue_id),
            )
            self._db.commit()

    def cancel_activation_for_session(
        self, session_id: str, *, now: int | None = None,
    ) -> None:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "UPDATE activation_waits SET state = 'canceled', updated_at = ? "
                "WHERE session_id = ? AND state IN ('waiting', 'resuming')",
                (now, session_id),
            )
            self._db.commit()

    def cancel_activation_for_issue(
        self, issue_id: str, *, now: int | None = None,
    ) -> None:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "UPDATE activation_waits SET state = 'canceled', updated_at = ? "
                "WHERE issue_id = ? AND state IN ('waiting', 'dispatch_unknown')",
                (now, issue_id),
            )
            self._db.commit()

    @staticmethod
    def direct_issue_fingerprint(team_id: str, title: str) -> str:
        if not team_id or not title:
            return ""
        return hashlib.sha256(f"{team_id}\0{title}".encode("utf-8")).hexdigest()

    def reserve_direct_activation_grant(
        self,
        *,
        operation_key: str,
        source_platform: str,
        source_user_id: str,
        source_message_id: str,
        source_session_id: str,
        source_profile: str,
        actor_id: str,
        team_id: str,
        issue_fingerprint: str,
        policy_result: str = "gateway_authorized_direct_dm",
        now: int | None = None,
    ) -> bool:
        """Persist metadata-safe direct instruction provenance before create dispatch."""
        values = (
            operation_key, source_platform, source_user_id, source_message_id,
            source_session_id, source_profile, policy_result, actor_id, team_id,
            issue_fingerprint,
        )
        if any(not isinstance(value, str) or not value for value in values):
            return False
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO direct_activation_grants("
                "operation_key, source_platform, source_user_id, source_message_id, "
                "source_session_id, source_profile, policy_result, actor_id, team_id, "
                "issue_fingerprint, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)",
                (*values, now, now),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def bind_direct_activation_grant(
        self, operation_key: str, issue_id: str, *, now: int | None = None,
    ) -> bool:
        """Bind a reserved provenance grant to the authoritative vendor issue ID."""
        if not operation_key or not issue_id:
            return False
        now = int(time.time()) if now is None else int(now)
        cutoff = now - self.processing_timeout_seconds
        with self._lock:
            self._db.execute(
                "UPDATE direct_activation_grants SET state='failed', "
                "last_error='unbound_reservation_expired', updated_at=? "
                "WHERE operation_key=? AND issue_id IS NULL AND state='reserved' "
                "AND updated_at <= ?",
                (now, operation_key, cutoff),
            )
            cursor = self._db.execute(
                "UPDATE direct_activation_grants SET issue_id=?, state='granted', "
                "last_error=NULL, updated_at=? WHERE operation_key=? AND ("
                "(state='reserved' AND issue_id IS NULL AND updated_at > ?) OR "
                "(state='granted' AND issue_id=?))",
                (issue_id, now, operation_key, cutoff, issue_id),
            )
            if cursor.rowcount == 1:
                self._db.execute(
                    "UPDATE direct_activation_grants SET state='canceled', "
                    "last_error='session_stopped_before_grant_binding', updated_at=? "
                    "WHERE operation_key=? AND EXISTS (SELECT 1 FROM direct_activation_events "
                    "WHERE direct_activation_events.issue_id=? "
                    "AND direct_activation_events.state='canceled')",
                    (now, operation_key, issue_id),
                )
            self._db.commit()
        return cursor.rowcount == 1

    def fail_direct_activation_grant(
        self, operation_key: str, error: str, *, now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cursor = self._db.execute(
                "UPDATE direct_activation_grants SET state='failed', last_error=?, updated_at=? "
                "WHERE operation_key=? AND state IN ('reserved', 'granted')",
                (str(error)[:1000], now, operation_key),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def has_unbound_direct_reservation(
        self,
        *,
        actor_id: str,
        team_id: str,
        issue_fingerprint: str,
        now: int | None = None,
    ) -> bool:
        if not all((actor_id, team_id, issue_fingerprint)):
            return False
        now = int(time.time()) if now is None else int(now)
        cutoff = now - self.processing_timeout_seconds
        with self._lock:
            self._db.execute(
                "UPDATE direct_activation_grants SET state='failed', "
                "last_error='unbound_reservation_expired', updated_at=? "
                "WHERE issue_id IS NULL AND state='reserved' AND updated_at <= ?",
                (now, cutoff),
            )
            row = self._db.execute(
                "SELECT COUNT(*) FROM direct_activation_grants WHERE issue_id IS NULL "
                "AND actor_id=? AND team_id=? AND issue_fingerprint=? "
                "AND state='reserved' AND updated_at > ?",
                (actor_id, team_id, issue_fingerprint, cutoff),
            ).fetchone()
            self._db.commit()
        # Early webhooks are keyed by authoritative issue/session IDs. Binding
        # the operation key to the returned issue later correlates concurrent
        # identical creates without guessing between their provenance records.
        return bool(row and int(row[0]) >= 1)

    def put_direct_activation_event(
        self,
        issue_id: str,
        session_id: str,
        delivery_key: str,
        prompt: dict[str, Any],
        *,
        now: int | None = None,
    ) -> bool:
        if not all((issue_id, session_id, delivery_key)):
            return False
        now = int(time.time()) if now is None else int(now)
        prompt_json = json.dumps(
            prompt, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO direct_activation_events("
                "issue_id, session_id, delivery_key, prompt_json, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'waiting', ?, ?)",
                (issue_id, session_id, delivery_key, prompt_json, now, now),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def get_direct_activation_event(self, issue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT issue_id, session_id, delivery_key, prompt_json, state, last_error, "
                "created_at, updated_at FROM direct_activation_events WHERE issue_id=?",
                (issue_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "issue_id": str(row[0]),
            "session_id": str(row[1]),
            "delivery_key": str(row[2]),
            "prompt": json.loads(row[3]),
            "state": str(row[4]),
            "last_error": row[5],
            "created_at": int(row[6]),
            "updated_at": int(row[7]),
        }

    def list_direct_activation_events(self) -> list[dict[str, Any]]:
        now = int(time.time())
        with self._lock:
            self._db.execute(
                "UPDATE direct_activation_events SET state='failed', "
                "last_error='unbound_event_expired', updated_at=? "
                "WHERE state='waiting' AND updated_at <= ?",
                (now, now - self.processing_timeout_seconds),
            )
            issue_ids = [
                str(row[0]) for row in self._db.execute(
                    "SELECT issue_id FROM direct_activation_events WHERE state='waiting' "
                    "ORDER BY created_at"
                ).fetchall()
            ]
            self._db.commit()
        return [
            event
            for issue_id in issue_ids
            if (event := self.get_direct_activation_event(issue_id)) is not None
        ]

    def mark_direct_activation_event(
        self,
        issue_id: str,
        state: str,
        *,
        error: str | None = None,
        now: int | None = None,
    ) -> bool:
        if state not in {"claimed", "dispatched", "failed"}:
            return False
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            if state == "failed":
                cursor = self._db.execute(
                    "UPDATE direct_activation_events SET state=?, last_error=?, updated_at=? "
                    "WHERE issue_id=? AND state IN ('waiting', 'claimed')",
                    (state, str(error)[:1000] if error else None, now, issue_id),
                )
            else:
                expected = "waiting" if state == "claimed" else "claimed"
                cursor = self._db.execute(
                    "UPDATE direct_activation_events SET state=?, last_error=?, updated_at=? "
                    "WHERE issue_id=? AND state=?",
                    (state, str(error)[:1000] if error else None, now, issue_id, expected),
                )
            self._db.commit()
        return cursor.rowcount == 1

    def cancel_direct_activation_for_session(
        self, session_id: str, *, now: int | None = None,
    ) -> bool:
        if not session_id:
            return False
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            grant_count = self._db.execute(
                "UPDATE direct_activation_grants SET state='canceled', "
                "last_error='session_stopped', updated_at=? "
                "WHERE session_id=? AND state='claimed'",
                (now, session_id),
            ).rowcount
            event_count = self._db.execute(
                "UPDATE direct_activation_events SET state='canceled', "
                "last_error='session_stopped', updated_at=? "
                "WHERE session_id=? AND state IN ('waiting', 'claimed')",
                (now, session_id),
            ).rowcount
            self._db.commit()
        return bool(grant_count or event_count)

    def get_direct_activation_grant(self, issue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT operation_key, issue_id, source_platform, source_user_id, "
                "source_message_id, source_session_id, source_profile, policy_result, "
                "actor_id, team_id, issue_fingerprint, "
                "session_id, activation_key, state, last_error, created_at, updated_at "
                "FROM direct_activation_grants WHERE issue_id=?",
                (issue_id,),
            ).fetchone()
        if row is None:
            return None
        keys = (
            "operation_key", "issue_id", "source_platform", "source_user_id",
            "source_message_id", "source_session_id", "source_profile", "policy_result",
            "actor_id", "team_id", "issue_fingerprint", "session_id", "activation_key",
            "state", "last_error",
            "created_at", "updated_at",
        )
        result: dict[str, Any] = dict(zip(keys, row, strict=True))
        result["created_at"] = int(result["created_at"])
        result["updated_at"] = int(result["updated_at"])
        return result

    def claim_direct_activation(
        self,
        issue_id: str,
        session_id: str,
        *,
        actor_id: str,
        team_id: str,
        now: int | None = None,
    ) -> bool:
        """Atomically fence one Direct AgentSession dispatch."""
        if not all((issue_id, session_id, actor_id, team_id)):
            return False
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT operation_key, actor_id, team_id, state FROM direct_activation_grants "
                "WHERE issue_id=?",
                (issue_id,),
            ).fetchone()
            if row is None or str(row[3]) != "granted" or not (
                hmac.compare_digest(str(row[1]), actor_id)
                and hmac.compare_digest(str(row[2]), team_id)
            ):
                self._db.rollback()
                return False
            activation_key = hashlib.sha256(
                "\0".join((str(row[0]), issue_id, session_id, actor_id, team_id)).encode()
            ).hexdigest()
            self._db.execute(
                "UPDATE direct_activation_grants SET state='claimed', session_id=?, "
                "activation_key=?, last_error=NULL, updated_at=? "
                "WHERE issue_id=? AND state='granted'",
                (session_id, activation_key, now, issue_id),
            )
            self._db.commit()
            return True

    def reset_direct_activation_claim(
        self, issue_id: str, session_id: str, error: str, *, now: int | None = None,
    ) -> bool:
        """Restore retryability only when Hermes dispatch was not attempted."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            grant_count = self._db.execute(
                "UPDATE direct_activation_grants SET state='granted', session_id='', "
                "activation_key='', last_error=?, updated_at=? "
                "WHERE issue_id=? AND session_id=? AND state='claimed'",
                (str(error)[:1000], now, issue_id, session_id),
            ).rowcount
            self._db.execute(
                "UPDATE direct_activation_events SET state='waiting', last_error=?, updated_at=? "
                "WHERE issue_id=? AND session_id=? AND state='claimed'",
                (str(error)[:1000], now, issue_id, session_id),
            )
            self._db.commit()
        return grant_count == 1

    def mark_direct_activation_unknown(
        self, issue_id: str, session_id: str, error: str, *, now: int | None = None,
    ) -> bool:
        """Fence an attempted dispatch whose acceptance outcome is unknown."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            grant_count = self._db.execute(
                "UPDATE direct_activation_grants SET state='dispatch_unknown', "
                "last_error=?, updated_at=? WHERE issue_id=? AND session_id=? "
                "AND state='claimed'",
                (str(error)[:1000], now, issue_id, session_id),
            ).rowcount
            self._db.execute(
                "UPDATE direct_activation_events SET state='dispatch_unknown', "
                "last_error=?, updated_at=? WHERE issue_id=? AND session_id=? "
                "AND state='claimed'",
                (str(error)[:1000], now, issue_id, session_id),
            )
            self._db.commit()
        return grant_count == 1

    def mark_direct_activation_dispatched(
        self, issue_id: str, session_id: str, *, now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cursor = self._db.execute(
                "UPDATE direct_activation_grants SET state='dispatched', updated_at=? "
                "WHERE issue_id=? AND session_id=? AND state='claimed'",
                (now, issue_id, session_id),
            )
            self._db.commit()
        return cursor.rowcount == 1

    def claim_manager_activation(
        self,
        issue_id: str,
        activation_key: str,
        evidence: dict[str, Any],
        *,
        now: int | None = None,
    ) -> bool:
        """Claim one Todo manager intake before mutating the issue delegate."""
        now = int(time.time()) if now is None else int(now)
        evidence_json = json.dumps(
            evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT activation_key, state FROM manager_activations WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
            if row is not None:
                same_key = str(row[0]) == activation_key
                retryable = str(row[1]) == "failed"
                if same_key and retryable:
                    self._db.execute(
                        "UPDATE manager_activations SET state='claimed', evidence_json=?, "
                        "last_error=NULL, updated_at=? WHERE issue_id=?",
                        (evidence_json, now, issue_id),
                    )
                    self._db.commit()
                    return True
                self._db.rollback()
                return False
            self._db.execute(
                "INSERT INTO manager_activations("
                "issue_id, activation_key, state, evidence_json, created_at, updated_at) "
                "VALUES (?, ?, 'claimed', ?, ?, ?)",
                (issue_id, activation_key, evidence_json, now, now),
            )
            self._db.commit()
            return True

    def claim_manager_reactivation(
        self,
        issue_id: str,
        activation_key: str,
        evidence: dict[str, Any],
        *,
        now: int | None = None,
    ) -> bool:
        """Claim one human terminal→started edge, replacing only a terminal prior activation."""
        now = int(time.time()) if now is None else int(now)
        evidence_json = json.dumps(
            evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT activation_key, state FROM manager_activations WHERE issue_id=?",
                (issue_id,),
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO manager_activations("
                    "issue_id, activation_key, state, session_id, evidence_json, "
                    "created_at, updated_at) VALUES (?, ?, 'claimed', NULL, ?, ?, ?)",
                    (issue_id, activation_key, evidence_json, now, now),
                )
                self._db.commit()
                return True
            if str(row[0]) == activation_key or str(row[1]) not in {
                "canceled", "session_started", "failed"
            }:
                self._db.rollback()
                return False
            self._db.execute(
                "UPDATE manager_activations SET activation_key=?, state='claimed', "
                "session_id=NULL, evidence_json=?, last_error=NULL, updated_at=? "
                "WHERE issue_id=?",
                (activation_key, evidence_json, now, issue_id),
            )
            self._db.commit()
            return True

    def get_manager_activation(self, issue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT issue_id, activation_key, state, session_id, evidence_json, "
                "last_error, created_at, updated_at FROM manager_activations WHERE issue_id=?",
                (issue_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "issue_id": str(row[0]),
            "activation_key": str(row[1]),
            "state": str(row[2]),
            "session_id": str(row[3]) if row[3] is not None else None,
            "evidence": json.loads(row[4]),
            "last_error": row[5],
            "created_at": int(row[6]),
            "updated_at": int(row[7]),
        }

    def mark_manager_activation(
        self,
        issue_id: str,
        state: str,
        *,
        session_id: str | None = None,
        error: str | None = None,
        now: int | None = None,
    ) -> None:
        if state not in {
            "delegation_unknown",
            "delegated",
            "dispatch_unknown",
            "session_started",
            "canceled",
            "failed",
        }:
            raise ValueError("invalid manager activation state")
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "UPDATE manager_activations SET state=?, session_id=COALESCE(?, session_id), "
                "last_error=?, updated_at=? WHERE issue_id=?",
                (state, session_id, error[:1000] if error else None, now, issue_id),
            )
            self._db.commit()

    def claim_manager_session(
        self, issue_id: str, session_id: str, *, now: int | None = None,
    ) -> bool:
        """CAS one delegated manager issue to one ambiguity-fenced session."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cur = self._db.execute(
                "UPDATE manager_activations SET state='dispatch_unknown', session_id=?, "
                "last_error=NULL, updated_at=? WHERE issue_id=? AND state='delegated'",
                (session_id, now, issue_id),
            )
            self._db.commit()
            return bool(cur.rowcount)

    def stage_pending_closure_event(
        self,
        issue_id: str,
        event_revision: float,
        event: dict[str, Any],
        *,
        now: int | None = None,
    ) -> None:
        """Durably fence an authoritative terminal event until its session is bound."""
        now = int(time.time()) if now is None else int(now)
        encoded = json.dumps(
            event, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO pending_closure_events("
                "issue_id, event_revision, event_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(issue_id) DO UPDATE SET "
                "event_revision=excluded.event_revision, event_json=excluded.event_json, "
                "updated_at=excluded.updated_at "
                "WHERE excluded.event_revision >= pending_closure_events.event_revision",
                (issue_id, float(event_revision), encoded, now, now),
            )
            self._db.commit()

    def get_pending_closure_event(self, issue_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT event_revision, event_json FROM pending_closure_events WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
        if row is None:
            return None
        return {"event_revision": float(row[0]), "event": json.loads(row[1])}

    def pending_closure_count(self) -> int:
        with self._lock:
            return int(
                self._db.execute("SELECT COUNT(*) FROM pending_closure_events").fetchone()[0]
            )

    def clear_pending_closure_event(self, issue_id: str, event_revision: float) -> bool:
        """Clear only the exact obsolete fence observed by the caller."""
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM pending_closure_events WHERE issue_id = ? AND event_revision = ?",
                (issue_id, float(event_revision)),
            )
            self._db.commit()
            return bool(cur.rowcount)

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

    def enqueue_outbox(
        self,
        item_id: str,
        aggregate_key: str,
        operation: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> bool:
        """Persist one operation. A stable item_id makes producer retries idempotent."""
        now = int(time.time()) if now is None else int(now)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute("SELECT 1 FROM outbox WHERE id = ?", (item_id,)).fetchone():
                self._db.rollback()
                return False
            sequence = int(
                self._db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM outbox WHERE aggregate_key = ?",
                    (aggregate_key,),
                ).fetchone()[0]
            )
            self._db.execute(
                "INSERT INTO outbox("
                "id, aggregate_key, sequence, operation, payload_json, state, attempts, "
                "next_attempt_at, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
                (item_id, aggregate_key, sequence, operation, encoded, now, now, now),
            )
            self._db.commit()
            return True

    def enqueue_closure_activity(
        self,
        closure_key: str,
        issue_id: str,
        session_id: str,
        activity_id: str,
        body: str,
        evidence: dict[str, Any],
        *,
        indicator_activity_id: str | None = None,
        indicator_body: str | None = None,
        now: int | None = None,
    ) -> bool:
        """Atomically persist closure evidence and its ordered Linear activities."""
        if bool(indicator_activity_id) != bool(indicator_body):
            raise ValueError("Closure indicator id and body must be provided together")
        now = int(time.time()) if now is None else int(now)
        outbox_id = f"activity:closure:{closure_key}"
        payload_json = json.dumps(
            {
                "activity_id": activity_id,
                "agent_session_id": session_id,
                "activity_type": "response",
                "body": body,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        indicator_outbox_id = f"activity:closure:indicator:{closure_key}"
        indicator_payload_json = (
            json.dumps(
                {
                    "activity_id": indicator_activity_id,
                    "agent_session_id": session_id,
                    "activity_type": "thought",
                    "body": indicator_body,
                    "ephemeral": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if indicator_activity_id and indicator_body
            else None
        )
        evidence_json = json.dumps(
            evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute(
                "SELECT 1 FROM closure_reconciliations WHERE closure_key = ?", (closure_key,)
            ).fetchone():
                self._db.rollback()
                return False
            sequence = int(
                self._db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM outbox WHERE aggregate_key = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            self._db.execute(
                "UPDATE waiting_executions SET state = 'canceled', updated_at = ? "
                "WHERE session_id = ? AND state IN ('waiting', 'resuming')",
                (now, session_id),
            )
            self._db.execute(
                "UPDATE activation_waits SET state = 'canceled', updated_at = ? "
                "WHERE session_id = ? AND state IN ('waiting', 'dispatch_unknown')",
                (now, session_id),
            )
            self._db.execute(
                "UPDATE manager_activations SET state='canceled', updated_at=? "
                "WHERE issue_id=? AND state IN "
                "('claimed', 'delegation_unknown', 'delegated', 'dispatch_unknown')",
                (now, issue_id),
            )
            self._db.execute(
                "UPDATE turn_decisions SET dispatch_state='fenced', outcome='stopped', "
                "error='authoritative_human_closure', updated_at=?, completed_at=? "
                "WHERE agent_session_id=? AND dispatch_state IN ('pending', 'enqueued')",
                (now, now, session_id),
            )
            self._db.execute(
                "UPDATE outbox SET state = 'delivered', "
                "last_error = 'Suppressed by authoritative human closure', "
                "updated_at = ?, delivered_at = ? "
                "WHERE aggregate_key = ? AND state IN ('pending', 'in_flight', 'dead')",
                (now, now, session_id),
            )
            if indicator_payload_json is not None:
                self._db.execute(
                    "INSERT INTO outbox("
                    "id, aggregate_key, sequence, operation, payload_json, state, attempts, "
                    "next_attempt_at, created_at, updated_at"
                    ") VALUES (?, ?, ?, 'activity.create', ?, 'pending', 0, ?, ?, ?)",
                    (
                        indicator_outbox_id,
                        session_id,
                        sequence,
                        indicator_payload_json,
                        now,
                        now,
                        now,
                    ),
                )
                sequence += 1
            self._db.execute(
                "INSERT INTO outbox("
                "id, aggregate_key, sequence, operation, payload_json, state, attempts, "
                "next_attempt_at, created_at, updated_at"
                ") VALUES (?, ?, ?, 'activity.create', ?, 'pending', 0, ?, ?, ?)",
                (outbox_id, session_id, sequence, payload_json, now, now, now),
            )
            self._db.execute(
                "INSERT INTO closure_reconciliations("
                "closure_key, issue_id, session_id, outbox_id, evidence_json, state, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (closure_key, issue_id, session_id, outbox_id, evidence_json, now, now),
            )
            self._db.execute(
                "DELETE FROM pending_closure_events WHERE issue_id = ?",
                (issue_id,),
            )
            self._db.commit()
            return True

    def get_closure(self, closure_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT closure_key, issue_id, session_id, outbox_id, evidence_json, state, "
                "last_error, created_at, updated_at, completed_at "
                "FROM closure_reconciliations WHERE closure_key = ?", (closure_key,)
            ).fetchone()
        if row is None:
            return None
        return {
            "closure_key": str(row[0]),
            "issue_id": str(row[1]),
            "session_id": str(row[2]),
            "outbox_id": str(row[3]),
            "evidence": json.loads(row[4]),
            "state": str(row[5]),
            "last_error": row[6],
            "created_at": int(row[7]),
            "updated_at": int(row[8]),
            "completed_at": int(row[9]) if row[9] is not None else None,
        }

    def has_session_closure(self, session_id: str) -> bool:
        with self._lock:
            return bool(
                self._db.execute(
                    "SELECT 1 FROM closure_reconciliations WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
            )

    def closure_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM closure_reconciliations GROUP BY state"
            ).fetchall()
            terminal_fences = int(
                self._db.execute(
                    "SELECT COUNT(*) FROM pending_closure_events p "
                    "LEFT JOIN issue_session_bindings b ON b.issue_id = p.issue_id "
                    "WHERE b.issue_id IS NULL"
                ).fetchone()[0]
            )
            blocked_dispatch = int(
                self._db.execute(
                    "SELECT COUNT(*) FROM pending_closure_events p "
                    "INNER JOIN issue_session_bindings b ON b.issue_id = p.issue_id"
                ).fetchone()[0]
            )
        result = {
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "terminal_fences": terminal_fences,
            "blocked_dispatch": blocked_dispatch,
            # Compatibility alias for pre-0.8 health consumers. It now means a
            # bound dispatch blocked on terminal verification, never an
            # optional unbound session.
            "pending_session_binding": blocked_dispatch,
        }
        result.update({str(state): int(count) for state, count in rows})
        return result

    def claim_due_outbox(self, *, now: float | None = None) -> OutboxItem | None:
        """Claim one due head-of-line item; dead activities block completion, dead status writes do not."""
        now = time.time() if now is None else float(now)
        stale_before = int(now - self.outbox_claim_timeout_seconds)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT o.id, o.aggregate_key, o.sequence, o.operation, o.payload_json, o.attempts "
                "FROM outbox o "
                "WHERE ((o.state = 'pending' AND o.next_attempt_at <= ?) "
                "OR (o.state = 'in_flight' AND o.updated_at < ?)) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM outbox earlier "
                "  WHERE earlier.aggregate_key = o.aggregate_key "
                "  AND earlier.sequence < o.sequence "
                "  AND (earlier.state IN ('pending', 'in_flight') "
                "       OR (earlier.state = 'dead' AND earlier.operation = 'activity.create'))"
                ") "
                "ORDER BY o.created_at, o.aggregate_key, o.sequence LIMIT 1",
                (now, stale_before),
            ).fetchone()
            if row is None:
                self._db.rollback()
                return None
            item_id, aggregate_key, sequence, operation, payload_json, attempts = row
            attempts = int(attempts) + 1
            self._db.execute(
                "UPDATE outbox SET state = 'in_flight', attempts = ?, updated_at = ? WHERE id = ?",
                (attempts, int(now), item_id),
            )
            self._db.commit()
            return OutboxItem(
                id=str(item_id),
                aggregate_key=str(aggregate_key),
                sequence=int(sequence),
                operation=str(operation),
                payload=json.loads(payload_json),
                attempts=attempts,
            )

    def mark_outbox_delivered(self, item_id: str, *, now: int | None = None) -> None:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "UPDATE outbox SET state = 'delivered', last_error = NULL, "
                "updated_at = ?, delivered_at = ? WHERE id = ?",
                (now, now, item_id),
            )
            self._db.execute(
                "UPDATE closure_reconciliations SET state = 'completed', last_error = NULL, "
                "updated_at = ?, completed_at = ? WHERE outbox_id = ?",
                (now, now, item_id),
            )
            self._db.commit()

    def reschedule_outbox(
        self,
        item_id: str,
        error: str,
        delay_seconds: float,
        *,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._db.execute(
                "UPDATE outbox SET state = 'pending', next_attempt_at = ?, last_error = ?, "
                "updated_at = ? WHERE id = ?",
                (now + max(0.0, delay_seconds), error[:1000], int(now), item_id),
            )
            self._db.commit()

    def dead_letter_outbox(
        self,
        item_id: str,
        error: str,
        *,
        closure_cleanup_activity_id: str | None = None,
        closure_cleanup_body: str | None = None,
        now: int | None = None,
    ) -> bool:
        """Dead-letter an item and atomically stage cleanup for a final closure."""
        if bool(closure_cleanup_activity_id) != bool(closure_cleanup_body):
            raise ValueError("Closure cleanup activity id and body must be provided together")
        now = int(time.time()) if now is None else int(now)
        truncated_error = error[:1000]
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE outbox SET state = 'dead', last_error = ?, updated_at = ? "
                    "WHERE id = ?",
                    (truncated_error, now, item_id),
                )
                closure = self._db.execute(
                    "SELECT closure_key, session_id FROM closure_reconciliations "
                    "WHERE outbox_id = ?",
                    (item_id,),
                ).fetchone()
                self._db.execute(
                    "UPDATE closure_reconciliations SET state = 'failed', last_error = ?, "
                    "updated_at = ? WHERE outbox_id = ?",
                    (truncated_error, now, item_id),
                )
                cleanup_inserted = False
                if closure is not None and closure_cleanup_activity_id and closure_cleanup_body:
                    closure_key, session_id = map(str, closure)
                    cleanup_id = f"activity:closure-error:{closure_key}"
                    cleanup_aggregate = f"closure-cleanup:{closure_key}"
                    cleanup_payload = json.dumps(
                        {
                            "activity_id": closure_cleanup_activity_id,
                            "agent_session_id": session_id,
                            "activity_type": "error",
                            "body": closure_cleanup_body,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    existing = self._db.execute(
                        "SELECT aggregate_key, operation, payload_json FROM outbox WHERE id = ?",
                        (cleanup_id,),
                    ).fetchone()
                    if existing is not None and tuple(existing) != (
                        cleanup_aggregate,
                        "activity.create",
                        cleanup_payload,
                    ):
                        raise sqlite3.IntegrityError(
                            f"Conflicting deterministic closure cleanup item: {cleanup_id}"
                        )
                    if existing is None:
                        sequence = int(
                            self._db.execute(
                                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM outbox "
                                "WHERE aggregate_key = ?",
                                (cleanup_aggregate,),
                            ).fetchone()[0]
                        )
                        self._db.execute(
                            "INSERT INTO outbox("
                            "id, aggregate_key, sequence, operation, payload_json, state, "
                            "attempts, next_attempt_at, created_at, updated_at"
                            ") VALUES (?, ?, ?, 'activity.create', ?, 'pending', 0, ?, ?, ?)",
                            (
                                cleanup_id,
                                cleanup_aggregate,
                                sequence,
                                cleanup_payload,
                                now,
                                now,
                                now,
                            ),
                        )
                        cleanup_inserted = True
                self._db.commit()
                return cleanup_inserted
            except Exception:
                self._db.rollback()
                raise

    def outbox_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM outbox GROUP BY state"
            ).fetchall()
        result = {"pending": 0, "in_flight": 0, "delivered": 0, "dead": 0}
        result.update({str(state): int(count) for state, count in rows})
        return result

    def get_outbox_item(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, aggregate_key, sequence, operation, payload_json, state, attempts, "
                "next_attempt_at, last_error FROM outbox WHERE id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "aggregate_key": row[1],
            "sequence": int(row[2]),
            "operation": row[3],
            "payload": json.loads(row[4]),
            "state": row[5],
            "attempts": int(row[6]),
            "next_attempt_at": float(row[7]),
            "last_error": row[8],
        }

    def latest_activity_progress_state(self, aggregate_key: str) -> dict[str, str]:
        """Return durable terminal-fence metadata from the newest session activity."""
        with self._lock:
            row = self._db.execute(
                "SELECT payload_json FROM outbox WHERE aggregate_key = ? "
                "AND operation IN ('activity.create', 'activity.transient.create') "
                "ORDER BY sequence DESC LIMIT 1",
                (aggregate_key,),
            ).fetchone()
        if row is None:
            return {"activity_type": "", "terminal_progress_key": ""}
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return {"activity_type": "", "terminal_progress_key": ""}
        return {
            "activity_type": str(payload.get("activity_type") or "").strip().lower(),
            "terminal_progress_key": str(
                payload.get("terminal_progress_key") or ""
            ).strip(),
        }

    def open_progress_turn(self, aggregate_key: str, turn_key: str) -> bool:
        """Persist a trusted turn; replay of the same fenced key stays fenced."""
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT turn_key, fenced FROM progress_turns WHERE aggregate_key = ?",
                (aggregate_key,),
            ).fetchone()
            if row is None:
                fenced = False
                self._db.execute(
                    "INSERT INTO progress_turns(aggregate_key, turn_key, fenced, updated_at) "
                    "VALUES (?, ?, 0, ?)",
                    (aggregate_key, turn_key, now),
                )
            elif hmac.compare_digest(str(row[0]), turn_key):
                fenced = bool(row[1])
            else:
                fenced = False
                self._db.execute(
                    "UPDATE progress_turns SET turn_key = ?, fenced = 0, updated_at = ? "
                    "WHERE aggregate_key = ?",
                    (turn_key, now, aggregate_key),
                )
            self._db.commit()
        return not fenced

    def ensure_progress_turn(self, aggregate_key: str, fallback_turn_key: str) -> str:
        """Atomically return the current key, inserting a terminal sentinel if absent."""
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT turn_key FROM progress_turns WHERE aggregate_key = ?",
                (aggregate_key,),
            ).fetchone()
            if row is not None:
                return str(row[0])
            self._db.execute(
                "INSERT INTO progress_turns(aggregate_key, turn_key, fenced, updated_at) "
                "VALUES (?, ?, 0, ?)",
                (aggregate_key, fallback_turn_key, now),
            )
            self._db.commit()
        return fallback_turn_key

    def current_progress_turn_key(self, aggregate_key: str) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT turn_key FROM progress_turns WHERE aggregate_key = ?",
                (aggregate_key,),
            ).fetchone()
        return str(row[0]) if row is not None else ""

    def progress_is_allowed(self, aggregate_key: str, turn_key: str = "") -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT turn_key, fenced FROM progress_turns WHERE aggregate_key = ?",
                (aggregate_key,),
            ).fetchone()
        if row is None:
            return True
        return (
            (not turn_key or hmac.compare_digest(str(row[0]), turn_key))
            and not bool(row[1])
        )

    def fence_progress_turn(self, aggregate_key: str, turn_key: str) -> None:
        """Fence only the turn that produced the terminal activity."""
        if not turn_key:
            return
        now = int(time.time())
        with self._lock:
            self._db.execute(
                "UPDATE progress_turns SET fenced = 1, updated_at = ? "
                "WHERE aggregate_key = ? AND turn_key = ?",
                (now, aggregate_key, turn_key),
            )
            self._db.commit()

    def requeue_dead_outbox(self, item_id: str, *, now: int | None = None) -> bool:
        """Return one inspected dead letter to the delivery queue."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cur = self._db.execute(
                "UPDATE outbox SET state = 'pending', next_attempt_at = ?, last_error = NULL, "
                "updated_at = ? WHERE id = ? AND state = 'dead'",
                (now, now, item_id),
            )
            if cur.rowcount:
                self._db.execute(
                    "UPDATE closure_reconciliations SET state = 'pending', last_error = NULL, "
                    "updated_at = ? WHERE outbox_id = ?",
                    (now, item_id),
                )
            self._db.commit()
            return bool(cur.rowcount)

    @staticmethod
    def _decode_wait(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "session_id": str(row[0]), "issue_id": str(row[1]),
            "delivery_key": str(row[2]), "prompt": json.loads(row[3]),
            "blockers": json.loads(row[4]), "state": str(row[5]),
            "revision": int(row[6]), "last_error": row[7],
            "created_at": int(row[8]), "updated_at": int(row[9]),
            "resumed_at": int(row[10]) if row[10] is not None else None,
        }

    def put_wait(
        self, session_id: str, issue_id: str, delivery_key: str,
        prompt: dict[str, Any], blockers: list[dict[str, Any]], *, now: int | None = None,
    ) -> None:
        """Persist blocked work before acknowledging awaitingInput to Linear."""
        now = int(time.time()) if now is None else int(now)
        prompt_json = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        blockers_json = json.dumps(blockers, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._lock:
            if self._db.execute(
                "SELECT 1 FROM closure_reconciliations WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone():
                return
            self._db.execute(
                "INSERT INTO waiting_executions("
                "session_id, issue_id, delivery_key, prompt_json, blockers_json, state, "
                "revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'waiting', 1, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET issue_id=excluded.issue_id, "
                "delivery_key=excluded.delivery_key, prompt_json=excluded.prompt_json, "
                "blockers_json=excluded.blockers_json, state='waiting', "
                "revision=waiting_executions.revision + 1, last_error=NULL, "
                "updated_at=excluded.updated_at, resumed_at=NULL",
                (session_id, issue_id, delivery_key, prompt_json, blockers_json, now, now),
            )
            self._db.commit()

    def get_wait(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT session_id, issue_id, delivery_key, prompt_json, blockers_json, state, "
                "revision, last_error, created_at, updated_at, resumed_at "
                "FROM waiting_executions WHERE session_id = ?", (session_id,),
            ).fetchone()
        return self._decode_wait(row) if row else None

    def list_waiting(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id, issue_id, delivery_key, prompt_json, blockers_json, state, "
                "revision, last_error, created_at, updated_at, resumed_at "
                "FROM waiting_executions WHERE state = 'waiting' ORDER BY created_at"
            ).fetchall()
        return [self._decode_wait(row) for row in rows]

    def find_waiting_by_blocker(self, blocker_issue_id: str) -> list[dict[str, Any]]:
        return [wait for wait in self.list_waiting() if blocker_issue_id in {
            str(item.get("id") or "") for item in wait["blockers"]
        }]

    def update_wait_blockers(
        self, session_id: str, blockers: list[dict[str, Any]], *, now: int | None = None,
    ) -> bool:
        now = int(time.time()) if now is None else int(now)
        encoded = json.dumps(blockers, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._lock:
            cur = self._db.execute(
                "UPDATE waiting_executions SET blockers_json = ?, revision = revision + 1, "
                "updated_at = ? WHERE session_id = ? AND state = 'waiting'",
                (encoded, now, session_id),
            )
            self._db.commit()
            return bool(cur.rowcount)

    def claim_wait(self, session_id: str, *, now: int | None = None) -> bool:
        """Grant exactly one resume worker ownership of a waiting session."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cur = self._db.execute(
                "UPDATE waiting_executions SET state = 'resuming', revision = revision + 1, "
                "updated_at = ? WHERE session_id = ? AND state = 'waiting'",
                (now, session_id),
            )
            self._db.commit()
            return bool(cur.rowcount)

    def mark_wait_resumed(self, session_id: str, *, now: int | None = None) -> None:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "UPDATE waiting_executions SET state = 'resumed', updated_at = ?, resumed_at = ? "
                "WHERE session_id = ? AND state = 'resuming'", (now, now, session_id),
            )
            self._db.commit()

    def fail_wait(self, session_id: str, error: str, *, now: int | None = None) -> None:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            self._db.execute(
                "UPDATE waiting_executions SET state = 'failed', last_error = ?, updated_at = ? "
                "WHERE session_id = ? AND state IN ('waiting', 'resuming')",
                (error[:1000], now, session_id),
            )
            self._db.commit()

    def cancel_wait(self, session_id: str, *, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cur = self._db.execute(
                "UPDATE waiting_executions SET state = 'canceled', updated_at = ? "
                "WHERE session_id = ? AND state IN ('waiting', 'resuming')", (now, session_id),
            )
            self._db.commit()
            return bool(cur.rowcount)

    def cancel_waits_for_issue(self, issue_id: str, *, now: int | None = None) -> int:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            cur = self._db.execute(
                "UPDATE waiting_executions SET state = 'canceled', updated_at = ? "
                "WHERE issue_id = ? AND state IN ('waiting', 'resuming')", (now, issue_id),
            )
            self._db.commit()
            return int(cur.rowcount)

    def waiting_counts(self, *, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM waiting_executions GROUP BY state"
            ).fetchall()
            oldest = self._db.execute(
                "SELECT MIN(created_at) FROM waiting_executions WHERE state = 'waiting'"
            ).fetchone()[0]
            error_row = self._db.execute(
                "SELECT last_error FROM waiting_executions WHERE last_error IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        result: dict[str, Any] = {
            "waiting": 0, "resuming": 0, "resumed": 0, "canceled": 0, "failed": 0,
            "oldest_wait_seconds": max(0, now - int(oldest)) if oldest is not None else None,
            "last_error": error_row[0] if error_row else None,
        }
        result.update({str(state): int(count) for state, count in rows})
        return result

    def activation_counts(self, *, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM activation_waits GROUP BY state"
            ).fetchall()
            oldest = self._db.execute(
                "SELECT MIN(created_at) FROM activation_waits WHERE state = 'waiting'"
            ).fetchone()[0]
            error_row = self._db.execute(
                "SELECT last_error FROM activation_waits WHERE last_error IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        result: dict[str, Any] = {
            "waiting": 0,
            "dispatch_unknown": 0,
            "resumed": 0,
            "canceled": 0,
            "failed": 0,
            "oldest_wait_seconds": max(0, now - int(oldest)) if oldest is not None else None,
            "last_error": error_row[0] if error_row else None,
        }
        result.update({str(state): int(count) for state, count in rows})
        return result

    def channel_route_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM channel_routes GROUP BY state"
            ).fetchall()
        return {str(state): int(count) for state, count in rows}

    def manager_activation_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM manager_activations GROUP BY state"
            ).fetchall()
        result = {
            "claimed": 0,
            "delegation_unknown": 0,
            "delegated": 0,
            "dispatch_unknown": 0,
            "session_started": 0,
            "canceled": 0,
            "failed": 0,
        }
        result.update({str(state): int(count) for state, count in rows})
        return result

    def direct_activation_counts(self, *, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            rows = self._db.execute(
                "SELECT state, COUNT(*) FROM direct_activation_grants GROUP BY state"
            ).fetchall()
            oldest = self._db.execute(
                "SELECT MIN(updated_at) FROM direct_activation_grants "
                "WHERE state IN ('reserved', 'granted', 'claimed')"
            ).fetchone()[0]
            stuck_active = self._db.execute(
                "SELECT COUNT(*) FROM direct_activation_grants "
                "WHERE state IN ('reserved', 'granted', 'claimed') AND updated_at <= ?",
                (now - self.processing_timeout_seconds,),
            ).fetchone()[0]
            waiting_events = self._db.execute(
                "SELECT COUNT(*) FROM direct_activation_events WHERE state='waiting'"
            ).fetchone()[0]
            stuck_events = self._db.execute(
                "SELECT COUNT(*) FROM direct_activation_events "
                "WHERE state IN ('waiting', 'claimed') AND updated_at <= ?",
                (now - self.processing_timeout_seconds,),
            ).fetchone()[0]
            error_row = self._db.execute(
                "SELECT last_error FROM direct_activation_grants WHERE last_error IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        result: dict[str, Any] = {
            "reserved": 0,
            "granted": 0,
            "claimed": 0,
            "dispatch_unknown": 0,
            "dispatched": 0,
            "canceled": 0,
            "failed": 0,
            "stuck_active": int(stuck_active),
            "waiting_events": int(waiting_events),
            "stuck_events": int(stuck_events),
            "oldest_active_seconds": (
                max(0, now - int(oldest)) if oldest is not None else None
            ),
            "last_error": error_row[0] if error_row else None,
        }
        result.update({str(state): int(count) for state, count in rows})
        return result

    def prune(self, *, now: int | None = None) -> int:
        now = int(time.time()) if now is None else int(now)
        cutoff = now - self.retention_seconds
        with self._lock:
            inbound = self._db.execute(
                "DELETE FROM deliveries WHERE state = 'done' AND updated_at < ?",
                (cutoff,),
            ).rowcount
            outbound = self._db.execute(
                "DELETE FROM outbox WHERE state = 'delivered' AND delivered_at < ?",
                (cutoff,),
            ).rowcount
            waits = self._db.execute(
                "DELETE FROM waiting_executions WHERE state IN ('resumed', 'canceled') "
                "AND updated_at < ?", (cutoff,),
            ).rowcount
            activations = self._db.execute(
                "DELETE FROM activation_waits WHERE state IN ('resumed', 'canceled') "
                "AND updated_at < ?", (cutoff,),
            ).rowcount
            managers = self._db.execute(
                "DELETE FROM manager_activations WHERE state IN ('session_started', 'canceled') "
                "AND updated_at < ?", (cutoff,),
            ).rowcount
            direct_grants = self._db.execute(
                "DELETE FROM direct_activation_grants "
                "WHERE state IN ('dispatched', 'canceled', 'failed') "
                "AND updated_at < ?", (cutoff,),
            ).rowcount
            direct_events = self._db.execute(
                "DELETE FROM direct_activation_events "
                "WHERE state IN ('dispatched', 'canceled', 'failed') "
                "AND updated_at < ?", (cutoff,),
            ).rowcount
            routes = self._db.execute(
                "DELETE FROM channel_routes WHERE state IN "
                "('dispatched', 'blocked', 'failed', 'ambiguous') AND updated_at < ?",
                (cutoff,),
            ).rowcount
            decisions = self._db.execute(
                "DELETE FROM turn_decisions WHERE dispatch_state IN ('completed', 'fenced') "
                "AND updated_at < ?", (cutoff,),
            ).rowcount
            self._db.commit()
            return (
                int(inbound) + int(outbound) + int(waits)
                + int(activations) + int(managers) + int(direct_grants)
                + int(direct_events) + int(routes) + int(decisions)
            )

    @staticmethod
    def _turn_decision_id(
        agent_session_id: str, goal_generation: int, ordinal: int
    ) -> str:
        material = f"{agent_session_id}\0{int(goal_generation)}\0{int(ordinal)}".encode()
        return "linear-turn-" + hashlib.sha256(material).hexdigest()

    @staticmethod
    def _turn_decision_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "decision_id": str(row[0]),
            "agent_session_id": str(row[1]),
            "issue_id": str(row[2]),
            "hermes_session_id": str(row[3]),
            "goal_generation": int(row[4]),
            "ordinal": int(row[5]),
            "outcome": str(row[6]),
            "dispatch_state": str(row[7]),
            "error": row[8],
            "created_at": int(row[9]),
            "updated_at": int(row[10]),
            "completed_at": int(row[11]) if row[11] is not None else None,
        }

    def reserve_turn_decision(
        self,
        agent_session_id: str,
        issue_id: str,
        hermes_session_id: str,
        goal_generation: int,
        ordinal: int,
        outcome: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Insert one deterministic decision, or return its exact prior row."""
        now = int(time.time()) if now is None else int(now)
        decision_id = self._turn_decision_id(agent_session_id, goal_generation, ordinal)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "INSERT OR IGNORE INTO turn_decisions("
                "decision_id, agent_session_id, issue_id, hermes_session_id, "
                "goal_generation, ordinal, outcome, dispatch_state, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    decision_id, agent_session_id, issue_id, hermes_session_id,
                    int(goal_generation), int(ordinal), outcome, now, now,
                ),
            )
            row = self._db.execute(
                "SELECT decision_id, agent_session_id, issue_id, hermes_session_id, "
                "goal_generation, ordinal, outcome, dispatch_state, error, created_at, "
                "updated_at, completed_at FROM turn_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            self._db.commit()
        assert row is not None
        result = self._turn_decision_dict(row)
        expected = {
            "agent_session_id": agent_session_id,
            "issue_id": issue_id,
            "hermes_session_id": hermes_session_id,
            "goal_generation": int(goal_generation),
            "ordinal": int(ordinal),
            "outcome": outcome,
        }
        if any(result[key] != value for key, value in expected.items()):
            raise sqlite3.IntegrityError("Conflicting deterministic Linear turn decision")
        return result

    def complete_turn_success(
        self,
        decision_id: str,
        item_id: str,
        aggregate_key: str,
        payload: dict[str, Any],
        *,
        now: int | None = None,
    ) -> bool:
        """Atomically persist the successful response and terminal decision."""
        now = int(time.time()) if now is None else int(now)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT outcome, dispatch_state, agent_session_id FROM turn_decisions "
                "WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if row is None or str(row[0]) != "success" or str(row[2]) != aggregate_key:
                self._db.rollback()
                raise sqlite3.IntegrityError("Successful Linear turn decision does not match")
            existing = self._db.execute(
                "SELECT aggregate_key, operation, payload_json FROM outbox WHERE id=?",
                (item_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (aggregate_key, "activity.create", encoded):
                    self._db.rollback()
                    raise sqlite3.IntegrityError("Conflicting successful Linear turn response")
            else:
                sequence = int(
                    self._db.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM outbox WHERE aggregate_key=?",
                        (aggregate_key,),
                    ).fetchone()[0]
                )
                self._db.execute(
                    "INSERT INTO outbox("
                    "id, aggregate_key, sequence, operation, payload_json, state, attempts, "
                    "next_attempt_at, created_at, updated_at"
                    ") VALUES (?, ?, ?, 'activity.create', ?, 'pending', 0, ?, ?, ?)",
                    (item_id, aggregate_key, sequence, encoded, now, now, now),
                )
            state = str(row[1])
            if state == "completed":
                self._db.commit()
                return False
            if state != "pending":
                self._db.rollback()
                raise sqlite3.IntegrityError("Successful Linear turn is not pending")
            changed = self._db.execute(
                "UPDATE turn_decisions SET dispatch_state='completed', error=NULL, "
                "updated_at=?, completed_at=? WHERE decision_id=? "
                "AND outcome='success' AND dispatch_state='pending'",
                (now, now, decision_id),
            ).rowcount
            if changed != 1:
                self._db.rollback()
                raise sqlite3.IntegrityError("Successful Linear turn completion raced")
            self._db.commit()
            return True

    def transition_turn_decision(
        self,
        decision_id: str,
        expected_state: str,
        new_state: str,
        *,
        error: str | None = None,
        now: int | None = None,
    ) -> bool:
        """Compare-and-swap a dispatch state without widening its replay window."""
        now = int(time.time()) if now is None else int(now)
        completed_at = now if new_state in {"completed", "fenced"} else None
        with self._lock:
            changed = self._db.execute(
                "UPDATE turn_decisions SET dispatch_state=?, error=?, updated_at=?, "
                "completed_at=? WHERE decision_id=? AND dispatch_state=?",
                (new_state, error[:1000] if error else None, now, completed_at,
                 decision_id, expected_state),
            ).rowcount
            self._db.commit()
        return bool(changed)

    def update_pending_turn_outcome(
        self,
        decision_id: str,
        expected_outcome: str,
        new_outcome: str,
        *,
        now: int | None = None,
    ) -> bool:
        """Classify a pre-reserved decision before any dispatch starts."""
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            changed = self._db.execute(
                "UPDATE turn_decisions SET outcome=?, updated_at=? "
                "WHERE decision_id=? AND dispatch_state='pending' AND outcome=?",
                (new_outcome, now, decision_id, expected_outcome),
            ).rowcount
            self._db.commit()
        return bool(changed)

    def get_turn_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT decision_id, agent_session_id, issue_id, hermes_session_id, "
                "goal_generation, ordinal, outcome, dispatch_state, error, created_at, "
                "updated_at, completed_at FROM turn_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        return self._turn_decision_dict(row) if row is not None else None

    def list_turn_decisions(self, agent_session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT decision_id, agent_session_id, issue_id, hermes_session_id, "
                "goal_generation, ordinal, outcome, dispatch_state, error, created_at, "
                "updated_at, completed_at FROM turn_decisions WHERE agent_session_id=? "
                "ORDER BY goal_generation, ordinal",
                (agent_session_id,),
            ).fetchall()
        return [self._turn_decision_dict(row) for row in rows]

    def recoverable_turn_decisions(
        self, *, limit: int = 50, after: tuple[int, str] | None = None
    ) -> list[dict[str, Any]]:
        after_created, after_id = after or (-1, "")
        with self._lock:
            rows = self._db.execute(
                "SELECT decision_id, agent_session_id, issue_id, hermes_session_id, "
                "goal_generation, ordinal, outcome, dispatch_state, error, created_at, "
                "updated_at, completed_at FROM turn_decisions "
                "WHERE dispatch_state IN ('pending', 'enqueued') "
                "AND (created_at > ? OR (created_at = ? AND decision_id > ?)) "
                "ORDER BY created_at, decision_id LIMIT ?",
                (after_created, after_created, after_id, max(1, min(int(limit), 250))),
            ).fetchall()
        return [self._turn_decision_dict(row) for row in rows]

    def running_turn_decisions(
        self, *, limit: int = 50, after: tuple[int, str] | None = None
    ) -> list[dict[str, Any]]:
        after_created, after_id = after or (-1, "")
        with self._lock:
            rows = self._db.execute(
                "SELECT decision_id, agent_session_id, issue_id, hermes_session_id, "
                "goal_generation, ordinal, outcome, dispatch_state, error, created_at, "
                "updated_at, completed_at FROM turn_decisions WHERE dispatch_state='running' "
                "AND (created_at > ? OR (created_at = ? AND decision_id > ?)) "
                "ORDER BY created_at, decision_id LIMIT ?",
                (after_created, after_created, after_id, max(1, min(int(limit), 250))),
            ).fetchall()
        return [self._turn_decision_dict(row) for row in rows]

    def fence_turn_decisions(
        self, agent_session_id: str, reason: str, *, now: int | None = None
    ) -> int:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            changed = self._db.execute(
                "UPDATE turn_decisions SET dispatch_state='fenced', outcome='stopped', "
                "error=?, updated_at=?, completed_at=? WHERE agent_session_id=? "
                "AND dispatch_state IN ('pending', 'enqueued', 'running')",
                (reason[:1000], now, now, agent_session_id),
            ).rowcount
            self._db.commit()
        return int(changed)

    def close(self) -> None:
        with self._lock:
            self._db.close()
