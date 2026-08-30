#!/usr/bin/env python3
"""Durable, fail-closed gateway restart queue and state machine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol
from urllib.parse import urlparse

ALLOWED_REQUESTERS = frozenset({"general", "coder"})
ALLOWED_TARGETS = frozenset(
    {"general", "assistant", "coder", "finance", "health", "marketing", "producer", "researcher", "writer"}
)
ACTIVE_STATES = ("queued", "preflight", "restarting", "verifying")


class RequestError(ValueError):
    """A request failed the coordinator's admission contract."""


class Runtime(Protocol):
    def pid(self, profile: str) -> int: ...
    def validate(self, profile: str) -> bool: ...
    def restart(self, profile: str) -> None: ...
    def managed(self, pid: int, profile: str) -> bool: ...
    def health(self, url: str) -> dict[str, Any]: ...


def requester_from_home(home: str | Path) -> str:
    path = Path(home)
    profile = path.name
    if profile not in ALLOWED_REQUESTERS or path.parent.name != "profiles":
        raise RequestError("requester_not_allowed")
    return profile


def requester_from_ancestry(ancestors: list[int], gateway_pids: dict[str, int]) -> str:
    matches = [profile for profile, pid in gateway_pids.items() if pid in ancestors]
    if not matches:
        raise RequestError("gateway_ancestor_not_found")
    if len(matches) != 1 or matches[0] not in ALLOWED_REQUESTERS:
        raise RequestError("requester_not_allowed")
    return matches[0]


class ProcessRuntime:
    """Production runtime with fixed launchd and Hermes command surfaces."""

    def __init__(
        self,
        uid: int | None = None,
        hermes: str = "/Users/mutlupolatcan/.local/bin/hermes",
        restart_timeout: float = 1860.0,
    ):
        self.uid = os.getuid() if uid is None else uid
        self.hermes = hermes
        self.restart_timeout = restart_timeout

    @staticmethod
    def parse_launchd_pid(output: str) -> int:
        match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", output, re.MULTILINE)
        if not match:
            raise RuntimeError("launchd_pid_missing")
        return int(match.group(1))

    def _label(self, profile: str) -> str:
        if profile not in ALLOWED_TARGETS:
            raise RuntimeError("invalid_profile")
        return f"gui/{self.uid}/ai.hermes.gateway-{profile}"

    def _launchd(self, profile: str) -> str:
        result = subprocess.run(
            ["/bin/launchctl", "print", self._label(profile)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return result.stdout

    def pid(self, profile: str) -> int:
        return self.parse_launchd_pid(self._launchd(profile))

    def gateway_pids(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for profile in ALLOWED_TARGETS:
            try:
                result[profile] = self.pid(profile)
            except (RuntimeError, subprocess.SubprocessError):
                continue
        return result

    @staticmethod
    def ancestry(start_pid: int | None = None) -> list[int]:
        current = os.getppid() if start_pid is None else start_pid
        result: list[int] = []
        for _ in range(32):
            if current <= 1 or current in result:
                break
            result.append(current)
            probe = subprocess.run(
                ["/bin/ps", "-p", str(current), "-o", "ppid="],
                check=True, capture_output=True, text=True, timeout=5,
            )
            current = int(probe.stdout.strip())
        if current == 1:
            result.append(1)
        return result

    def validate(self, profile: str) -> bool:
        result = subprocess.run(
            [self.hermes, "-p", profile, "config", "check"],
            capture_output=True, text=True, timeout=30,
            env={"PATH": "/Users/mutlupolatcan/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "HOME": "/Users/mutlupolatcan"},
        )
        return result.returncode == 0

    def restart(self, profile: str) -> None:
        old_pid = self.pid(profile)
        os.kill(old_pid, signal.SIGUSR1)
        deadline = time.monotonic() + self.restart_timeout
        while time.monotonic() < deadline:
            try:
                new_pid = self.pid(profile)
                if new_pid != old_pid:
                    time.sleep(1)
                    if self.pid(profile) == new_pid:
                        return
            except (RuntimeError, subprocess.SubprocessError):
                pass
            time.sleep(1)
        raise RuntimeError("graceful_restart_readiness_timeout")

    def managed(self, pid: int, profile: str) -> bool:
        try:
            launchd_pid = self.pid(profile)
            process = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], check=True, capture_output=True, text=True, timeout=5)
        except (RuntimeError, subprocess.SubprocessError):
            return False
        return launchd_pid == pid and bool(process.stdout.strip())

    def health(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("health_url_not_loopback")
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise RuntimeError("invalid_health_payload")
        return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_valid(path: Path) -> bool:
    try:
        if path.suffix == ".py":
            compile(path.read_bytes(), str(path), "exec")
        return True
    except (OSError, SyntaxError, ValueError):
        return False


def _coordinate_valid(payload: dict[str, Any], prefix: str) -> bool:
    path = Path(payload[f"{prefix}_path"])
    metadata = path.stat()
    identity = payload.get(f"{prefix}_identity")
    if identity is not None:
        current = {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
        }
        if current != identity or metadata.st_mode & 0o222:
            return False
    return _sha256(path) == payload[f"{prefix}_sha256"]


def _validated_payload(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "task_id", "target_profile", "artifact_path", "artifact_sha256", "expected_version",
        "expected_pid", "rollback_path", "rollback_sha256", "health_url", "semantic_canary",
    }
    if not required.issubset(payload):
        raise RequestError("missing_required_field")
    if not isinstance(payload["task_id"], str) or not payload["task_id"].strip():
        raise RequestError("invalid_task_id")
    if payload["target_profile"] not in ALLOWED_TARGETS:
        raise RequestError("invalid_target_profile")
    expected_pid = payload["expected_pid"]
    dependency = payload.get("dependency_task_id")
    if expected_pid == "dependency_new_pid":
        if not isinstance(dependency, str) or not dependency:
            raise RequestError("dependency_pid_without_dependency")
    elif not isinstance(expected_pid, int) or expected_pid <= 0:
        raise RequestError("invalid_expected_pid")
    if not isinstance(payload["expected_version"], str) or not payload["expected_version"]:
        raise RequestError("invalid_expected_version")
    for prefix in ("artifact", "rollback"):
        path = Path(payload[f"{prefix}_path"])
        expected = payload[f"{prefix}_sha256"]
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise RequestError(f"invalid_{prefix}_path")
        if not isinstance(expected, str) or len(expected) != 64 or _sha256(path) != expected.lower():
            raise RequestError(f"{prefix}_hash_mismatch")
    parsed = urlparse(payload["health_url"])
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RequestError("health_url_not_loopback")
    canary = payload["semantic_canary"]
    if not isinstance(canary, dict) or set(canary) != {"path", "equals"} or not isinstance(canary["path"], str):
        raise RequestError("invalid_semantic_canary")
    dependency = payload.get("dependency_task_id")
    if dependency is not None and (not isinstance(dependency, str) or dependency == payload["task_id"]):
        raise RequestError("invalid_dependency")
    barrier = payload.get("barrier", "activate-before-continue")
    if barrier != "activate-before-continue":
        raise RequestError("invalid_barrier")
    result = dict(payload)
    result["barrier"] = barrier
    return result


class CoordinatorStore:
    def __init__(self, path: str | Path, allowed_artifact_roots: list[str | Path] | None = None):
        self.path = Path(path)
        self.allowed_artifact_roots = tuple(Path(root).resolve() for root in allowed_artifact_roots or ())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA fullfsync=ON")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    requester TEXT NOT NULL,
                    target_profile TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    expected_version TEXT NOT NULL,
                    contract_sha256 TEXT NOT NULL,
                    dependency_task_id TEXT,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    leader_id INTEGER REFERENCES requests(id),
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS requests_status_id ON requests(status, id);
                CREATE TABLE IF NOT EXISTS ledger (
                    id INTEGER PRIMARY KEY,
                    request_id INTEGER NOT NULL REFERENCES requests(id),
                    state TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY,
                    request_id INTEGER NOT NULL REFERENCES requests(id),
                    event_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        os.chmod(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                os.chmod(sidecar, 0o600)

    def enqueue(self, requester: str, payload: dict[str, Any]) -> dict[str, Any]:
        if requester not in ALLOWED_REQUESTERS:
            raise RequestError("requester_not_allowed")
        clean = _validated_payload(payload)
        if self.allowed_artifact_roots:
            for prefix in ("artifact", "rollback"):
                supplied = Path(clean[f"{prefix}_path"])
                lexical = Path(os.path.abspath(supplied))
                resolved = supplied.resolve(strict=True)
                if lexical != resolved:
                    raise RequestError(f"{prefix}_path_contains_symlink")
                if not any(resolved.is_relative_to(root) for root in self.allowed_artifact_roots):
                    raise RequestError(f"{prefix}_path_outside_allowed_roots")
                metadata = resolved.stat()
                if metadata.st_mode & 0o222:
                    raise RequestError(f"{prefix}_not_immutable")
                clean[f"{prefix}_path"] = str(resolved)
                clean[f"{prefix}_identity"] = {
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "size": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                }
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        contract = dict(clean)
        contract.pop("task_id")
        contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM requests WHERE task_id=?", (clean["task_id"],)).fetchone():
                raise RequestError("duplicate_task_id")
            dependency = clean.get("dependency_task_id")
            if dependency:
                parent = conn.execute(
                    "SELECT target_profile,status FROM requests WHERE task_id=?", (dependency,)
                ).fetchone()
                if parent is None:
                    raise RequestError("dependency_not_found")
                if clean["expected_pid"] == "dependency_new_pid" and parent["target_profile"] != clean["target_profile"]:
                    raise RequestError("dependency_pid_cross_profile")
                if parent["status"] in {"operator_required", "superseded"}:
                    raise RequestError("dependency_already_failed")
            leader = conn.execute(
                """SELECT id FROM requests WHERE contract_sha256=?
                   AND status IN ('queued','preflight','restarting','verifying')
                   ORDER BY id LIMIT 1""",
                (contract_hash,),
            ).fetchone()
            status = "coalesced" if leader else "queued"
            leader_id = leader["id"] if leader else None
            if not leader:
                superseded = conn.execute(
                    """SELECT r.id,r.task_id,r.evidence_json FROM requests r
                       WHERE r.target_profile=? AND r.status='queued' AND r.dependency_task_id IS NULL
                         AND (? IS NULL OR r.task_id<>?)
                         AND NOT EXISTS (SELECT 1 FROM requests f WHERE f.leader_id=r.id)
                         AND NOT EXISTS (SELECT 1 FROM requests d WHERE d.dependency_task_id=r.task_id)""",
                    (clean["target_profile"], dependency, dependency),
                ).fetchall()
                for old in superseded:
                    evidence = json.loads(old["evidence_json"])
                    evidence.update({"reason": "superseded_by_newer_request", "superseded_by": clean["task_id"]})
                    encoded_evidence = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
                    conn.execute(
                        "UPDATE requests SET status='superseded',evidence_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'",
                        (encoded_evidence, old["id"]),
                    )
                    conn.execute(
                        "INSERT INTO ledger(request_id,state,evidence_json) VALUES(?,?,?)",
                        (old["id"], "superseded", json.dumps({"superseded_by": clean["task_id"]}, sort_keys=True)),
                    )
                    key = f"restart-terminal:{old['task_id']}:superseded"
                    conn.execute(
                        "INSERT INTO outbox(request_id,event_type,idempotency_key,payload_json) VALUES(?,?,?,?)",
                        (old["id"], "restart_terminal", key, json.dumps({"task_id": old["task_id"], "status": "superseded", "idempotency_key": key, **evidence}, sort_keys=True)),
                    )
            cursor = conn.execute(
                """INSERT INTO requests(task_id,requester,target_profile,artifact_sha256,expected_version,
                   contract_sha256,dependency_task_id,payload_json,status,leader_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (clean["task_id"], requester, clean["target_profile"], clean["artifact_sha256"],
                 clean["expected_version"], contract_hash, clean.get("dependency_task_id"), encoded, status, leader_id),
            )
            request_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO ledger(request_id,state,evidence_json) VALUES(?,?,?)",
                (request_id, status, json.dumps({"requester": requester}, sort_keys=True)),
            )
        result = {"id": request_id, "task_id": clean["task_id"], "status": status}
        if leader_id:
            result["leader_id"] = leader_id
        return result

    def _decode(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result.update(json.loads(result.pop("evidence_json")))
        return result

    def get(self, task_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM requests WHERE task_id=?", (task_id,)).fetchone()
        decoded = self._decode(row)
        if decoded is None:
            raise RequestError("request_not_found")
        return decoded

    def resolve_expected_pid(self, task_id: str) -> int:
        request = self.get(task_id)
        expected = request["payload"]["expected_pid"]
        if isinstance(expected, int):
            return expected
        dependency = request["dependency_task_id"]
        if not dependency:
            raise RequestError("dependency_pid_without_dependency")
        parent = self.get(dependency)
        if parent["status"] != "succeeded" or not isinstance(parent.get("new_pid"), int):
            raise RequestError("dependency_pid_unavailable")
        return int(parent["new_pid"])

    def settle_failed_dependencies(self) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.task_id,d.task_id dependency_task_id,d.status dependency_status
                   FROM requests r JOIN requests d ON d.task_id=r.dependency_task_id
                   WHERE r.status='queued' AND d.status IN ('operator_required','superseded')
                   ORDER BY r.id"""
            ).fetchall()
        for row in rows:
            self.transition(
                row["task_id"],
                "operator_required",
                {
                    "reason": "dependency_failed",
                    "dependency_task_id": row["dependency_task_id"],
                    "dependency_status": row["dependency_status"],
                },
            )
        return len(rows)

    def next_ready(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT r.* FROM requests r LEFT JOIN requests d ON d.task_id=r.dependency_task_id
                   WHERE r.status='queued' AND (r.dependency_task_id IS NULL OR d.status='succeeded')
                   ORDER BY r.id LIMIT 1"""
            ).fetchone()
        return self._decode(row)

    def claim_next(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT r.* FROM requests r LEFT JOIN requests d ON d.task_id=r.dependency_task_id
                   WHERE r.status='queued' AND (r.dependency_task_id IS NULL OR d.status='succeeded')
                   ORDER BY r.id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            changed = conn.execute(
                "UPDATE requests SET status='preflight',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'",
                (row["id"],),
            ).rowcount
            if changed != 1:
                return None
            conn.execute(
                "INSERT INTO ledger(request_id,state,evidence_json) VALUES(?,?,?)",
                (row["id"], "preflight", "{}"),
            )
            claimed = conn.execute("SELECT * FROM requests WHERE id=?", (row["id"],)).fetchone()
        return self._decode(claimed)

    def inflight(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE status IN ('preflight','restarting','verifying') ORDER BY id LIMIT 1"
            ).fetchone()
        return self._decode(row)

    def transition(self, task_id: str, state: str, evidence: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id,evidence_json FROM requests WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise RequestError("request_not_found")
            targets = [row]
            if state in {"succeeded", "operator_required"}:
                targets.extend(conn.execute("SELECT id,evidence_json FROM requests WHERE leader_id=?", (row["id"],)).fetchall())
            for target in targets:
                merged = json.loads(target["evidence_json"])
                merged.update(evidence)
                encoded = json.dumps(merged, sort_keys=True, separators=(",", ":"))
                conn.execute(
                    "UPDATE requests SET status=?,evidence_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (state, encoded, target["id"]),
                )
                conn.execute(
                    "INSERT INTO ledger(request_id,state,evidence_json) VALUES(?,?,?)",
                    (target["id"], state, json.dumps(evidence, sort_keys=True, separators=(",", ":"))),
                )
                if state in {"succeeded", "operator_required"}:
                    target_task = conn.execute("SELECT task_id FROM requests WHERE id=?", (target["id"],)).fetchone()["task_id"]
                    idempotency_key = f"restart-terminal:{target_task}:{state}"
                    conn.execute(
                        "INSERT INTO outbox(request_id,event_type,idempotency_key,payload_json) VALUES(?,?,?,?)",
                        (target["id"], "restart_terminal", idempotency_key,
                         json.dumps({"task_id": target_task, "status": state, "idempotency_key": idempotency_key, **merged}, sort_keys=True)),
                    )
        return self.get(task_id)

    def ledger(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT l.* FROM ledger l JOIN requests r ON r.id=l.request_id WHERE r.task_id=? ORDER BY l.id",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def integrity(self) -> str:
        with self._connect() as conn:
            return str(conn.execute("PRAGMA quick_check").fetchone()[0])

    def outbox_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "delivered": 0, "dead": 0}
        with self._connect() as conn:
            for row in conn.execute("SELECT status,COUNT(*) count FROM outbox GROUP BY status"):
                counts[row["status"]] = row["count"]
        return counts

    def pending_outbox(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,idempotency_key,payload_json FROM outbox WHERE status='pending' ORDER BY id"
            ).fetchall()
        return [{"id": row["id"], "idempotency_key": row["idempotency_key"], "payload": json.loads(row["payload_json"])} for row in rows]

    def ack_outbox(self, event_id: int) -> None:
        with self._connect() as conn:
            changed = conn.execute("UPDATE outbox SET status='delivered' WHERE id=? AND status='pending'", (event_id,)).rowcount
            if changed != 1:
                raise RequestError("outbox_ack_conflict")


class Coordinator:
    def __init__(
        self,
        store: CoordinatorStore,
        runtime: Runtime,
        readiness_attempts: int = 30,
        readiness_delay: float = 1.0,
    ):
        self.store = store
        self.runtime = runtime
        self.readiness_attempts = max(1, readiness_attempts)
        self.readiness_delay = max(0.0, readiness_delay)

    @staticmethod
    def _lookup(payload: dict[str, Any], dotted: str) -> Any:
        current: Any = payload
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                raise KeyError(dotted)
            current = current[part]
        return current

    def _verify(self, request: dict[str, Any], old_pid: int) -> dict[str, Any]:
        task_id = request["task_id"]
        payload = request["payload"]
        profile = request["target_profile"]
        new_pid: int | None = None
        health: dict[str, Any] = {}
        rollback_verified = False
        accepted = False
        attempts_used = 0
        last_error_type: str | None = None
        for attempt in range(1, self.readiness_attempts + 1):
            attempts_used = attempt
            try:
                new_pid = self.runtime.pid(profile)
                if attempt == 1:
                    self.store.transition(task_id, "verifying", {"new_pid": new_pid})
                health = self.runtime.health(payload["health_url"])
                canary = payload["semantic_canary"]
                rollback_verified = _coordinate_valid(payload, "rollback")
                accepted = (
                    new_pid != old_pid
                    and self.runtime.managed(new_pid, profile)
                    and _coordinate_valid(payload, "artifact")
                    and rollback_verified
                    and health.get("version") == payload["expected_version"]
                    and self._lookup(health, canary["path"]) == canary["equals"]
                )
                if accepted:
                    break
            except Exception as exc:
                last_error_type = type(exc).__name__
                health = {}
            if attempt < self.readiness_attempts:
                time.sleep(self.readiness_delay)
        try:
            rollback_verified = _coordinate_valid(payload, "rollback")
        except Exception:
            rollback_verified = False
        evidence = {
            "old_pid": old_pid,
            "new_pid": new_pid,
            "health": health,
            "readiness_attempts": attempts_used,
        }
        if accepted:
            return self.store.transition(task_id, "succeeded", evidence)
        evidence["reason"] = "post_restart_acceptance_failed"
        evidence["rollback_coordinate_verified"] = rollback_verified
        if last_error_type:
            evidence["error_type"] = last_error_type
        return self.store.transition(task_id, "operator_required", evidence)

    def _execute_claimed(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = request["task_id"]
        payload = request["payload"]
        profile = request["target_profile"]
        try:
            old_pid = self.runtime.pid(profile)
            valid_coordinates = (
                _coordinate_valid(payload, "artifact")
                and _coordinate_valid(payload, "rollback")
                and _artifact_valid(Path(payload["artifact_path"]))
            )
            expected_pid = self.store.resolve_expected_pid(task_id)
            config_valid = self.runtime.validate(profile)
        except Exception as exc:
            return self.store.transition(
                task_id,
                "operator_required",
                {"reason": "preflight_exception", "error_type": type(exc).__name__},
            )
        if old_pid != expected_pid or not valid_coordinates or not config_valid:
            return self.store.transition(task_id, "operator_required", {"reason": "preflight_failed", "old_pid": old_pid})
        self.store.transition(task_id, "restarting", {"old_pid": old_pid})
        self.runtime.restart(profile)
        return self._verify(self.store.get(task_id), old_pid)

    def process_once(self) -> dict[str, Any] | None:
        self.store.settle_failed_dependencies()
        if self.store.inflight():
            return self.recover_inflight()
        request = self.store.claim_next()
        if request is None:
            return None
        return self._execute_claimed(request)

    def recover_inflight(self) -> dict[str, Any] | None:
        request = self.store.inflight()
        if request is None:
            return None
        task_id = request["task_id"]
        if request["status"] == "preflight":
            return self._execute_claimed(request)
        if request["status"] == "restarting":
            old_pid = int(request["old_pid"])
            try:
                current_pid = self.runtime.pid(request["target_profile"])
            except Exception as exc:
                return self.store.transition(
                    task_id,
                    "operator_required",
                    {"reason": "restart_recovery_probe_failed", "error_type": type(exc).__name__},
                )
            if current_pid == old_pid:
                return self.store.transition(task_id, "operator_required", {"reason": "ambiguous_restart_unchanged_pid"})
            return self._verify(request, old_pid)
        if request["status"] == "verifying" and "old_pid" in request:
            return self._verify(request, int(request["old_pid"]))
        return self.store.transition(task_id, "operator_required", {"reason": f"ambiguous_recovery_{request['status']}"})
