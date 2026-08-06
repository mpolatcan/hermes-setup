"""Persistent deduplication and writeback outbox for Linear."""

from __future__ import annotations

import json
import sqlite3
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
        self.processing_timeout_seconds = processing_timeout_seconds
        self.retention_seconds = retention_seconds
        self.outbox_claim_timeout_seconds = outbox_claim_timeout_seconds
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
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
            "CREATE TABLE IF NOT EXISTS issue_session_bindings ("
            "issue_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
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
        if int(self._db.execute("PRAGMA user_version").fetchone()[0]) < 4:
            self._db.execute("PRAGMA user_version=4")
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
        if startup_recovery:
            self.prune()

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
            self._db.commit()
            return int(inbound) + int(outbound) + int(waits)

    def close(self) -> None:
        with self._lock:
            self._db.close()
