"""Content-minimizing idempotency ledger for outbound Linear MCP mutations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class OutboundLedgerError(RuntimeError):
    pass


class FleetGlobalLockError(RuntimeError):
    pass


def _canonical_fleet_locks_root() -> Path:
    """Resolve the shared fleet root without trusting profile-redirected HOME."""

    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        profile_root = Path(hermes_home)
        if (
            not profile_root.is_absolute()
            or profile_root.name in {"", ".", ".."}
            or profile_root.parent.name != "profiles"
        ):
            raise FleetGlobalLockError("HERMES_HOME is not a profile-scoped absolute path")
        return profile_root.parent.parent / "state" / "locks"
    return Path.home() / ".hermes" / "state" / "locks"


class FleetGlobalLock:
    """Pre-provisioned fleet lock with atomically persisted unresolved-create state."""

    _CANONICAL_FILENAME = "linear-quota-admission.lock"
    _CANONICAL_STATE_FILENAME = "linear-quota-admission-state.json"
    _EMPTY_STATE = {"version": 1, "unresolved_create_fences": []}
    _MAX_STATE_BYTES = 1024 * 1024

    def __init__(
        self,
        lock_path: str,
        *,
        canonical_locks_root: Path | None = None,
    ) -> None:
        supplied = Path(lock_path)
        if not supplied.is_absolute() or supplied.name in {"", ".", ".."}:
            raise FleetGlobalLockError("Fleet-global lock path must be absolute")
        if supplied.name != self._CANONICAL_FILENAME:
            raise FleetGlobalLockError("Fleet-global lock filename is not canonical")
        configured_root = canonical_locks_root or _canonical_fleet_locks_root()
        try:
            root = configured_root.resolve(strict=True)
            root_stat = root.stat()
            parent = supplied.parent.resolve(strict=True)
            parent_stat = parent.stat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise FleetGlobalLockError("Fleet-global lock parent is unavailable") from exc
        if parent != root or (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or stat.S_IMODE(root_stat.st_mode) != 0o700
            or (parent_stat.st_dev, parent_stat.st_ino)
            != (root_stat.st_dev, root_stat.st_ino)
        ):
            raise FleetGlobalLockError(
                "Fleet-global lock parent must be the private canonical locks root"
            )
        self.path = parent / supplied.name
        self.state_path = parent / self._CANONICAL_STATE_FILENAME
        self._parent = parent
        self._name = supplied.name
        self._state_name = self._CANONICAL_STATE_FILENAME
        self._parent_identity = (parent_stat.st_dev, parent_stat.st_ino)

    def acquire(self, *, blocking: bool = True) -> int | None:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        file_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        dir_fd: int | None = None
        fd: int | None = None
        try:
            dir_fd = os.open(self._parent, directory_flags)
            opened_parent = os.fstat(dir_fd)
            if (
                (opened_parent.st_dev, opened_parent.st_ino) != self._parent_identity
                or not stat.S_ISDIR(opened_parent.st_mode)
                or opened_parent.st_uid != os.getuid()
                or stat.S_IMODE(opened_parent.st_mode) != 0o700
            ):
                raise FleetGlobalLockError("Fleet-global lock parent identity is unsafe")
            before = os.stat(self._name, dir_fd=dir_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise FleetGlobalLockError("Fleet-global lock file is unsafe")
            fd = os.open(self._name, file_flags, dir_fd=dir_fd)
            opened = os.fstat(fd)
            after = os.stat(self._name, dir_fd=dir_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise FleetGlobalLockError("Fleet-global lock file identity is unsafe")
            lock_flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(fd, lock_flags)
            except BlockingIOError:
                os.close(fd)
                fd = None
                return None
            locked_path = os.stat(self._name, dir_fd=dir_fd, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (locked_path.st_dev, locked_path.st_ino):
                raise FleetGlobalLockError("Fleet-global lock file changed during acquisition")
            try:
                self._read_state_fd(fd)
            except FileNotFoundError:
                self._write_state_fd(fd, self._EMPTY_STATE)
            return fd
        except FleetGlobalLockError:
            if fd is not None:
                os.close(fd)
            raise
        except (OSError, ValueError) as exc:
            if fd is not None:
                os.close(fd)
            raise FleetGlobalLockError("Fleet-global lock is unavailable or unsafe") from exc
        finally:
            if dir_fd is not None:
                os.close(dir_fd)

    @staticmethod
    def release(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @staticmethod
    def _validate_state(state: Any) -> dict[str, Any]:
        if (
            not isinstance(state, dict)
            or set(state) != {"version", "unresolved_create_fences"}
            or type(state.get("version")) is not int
            or state["version"] != 1
            or not isinstance(state.get("unresolved_create_fences"), list)
        ):
            raise FleetGlobalLockError("Fleet admission state schema is invalid")
        fences = state["unresolved_create_fences"]
        if len(fences) > 10_000:
            raise FleetGlobalLockError("Fleet admission state is too large")
        seen: set[tuple[str, str]] = set()
        for fence in fences:
            if not isinstance(fence, dict) or set(fence) != {
                "operation_key_sha256",
                "observed_current_count",
                "profile_id",
                "timestamp",
            }:
                raise FleetGlobalLockError("Fleet admission fence schema is invalid")
            digest = fence.get("operation_key_sha256")
            count = fence.get("observed_current_count")
            profile_id = fence.get("profile_id")
            timestamp = fence.get("timestamp")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or type(count) is not int
                or count < 0
                or not isinstance(profile_id, str)
                or not profile_id
                or len(profile_id) > 200
                or any(ord(character) < 0x20 for character in profile_id)
                or type(timestamp) is not int
                or timestamp < 0
            ):
                raise FleetGlobalLockError("Fleet admission fence value is invalid")
            if (profile_id, digest) in seen:
                raise FleetGlobalLockError("Fleet admission fence is duplicated")
            seen.add((profile_id, digest))
        return state

    def _read_state_fd(self, fd: int) -> dict[str, Any]:
        del fd  # The caller holds the canonical fleet lock while state is read.
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        state_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        dir_fd: int | None = None
        state_fd: int | None = None
        try:
            dir_fd = os.open(self._parent, directory_flags)
            parent = os.fstat(dir_fd)
            if (
                (parent.st_dev, parent.st_ino) != self._parent_identity
                or stat.S_IMODE(parent.st_mode) != 0o700
                or parent.st_uid != os.getuid()
            ):
                raise FleetGlobalLockError("Fleet admission state parent is unsafe")
            state_fd = os.open(self._state_name, state_flags, dir_fd=dir_fd)
            opened = os.fstat(state_fd)
            path_stat = os.stat(self._state_name, dir_fd=dir_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                or opened.st_size <= 0
                or opened.st_size > self._MAX_STATE_BYTES
            ):
                raise FleetGlobalLockError("Fleet admission state identity is unsafe")
            payload = os.pread(state_fd, opened.st_size, 0)
            final = os.fstat(state_fd)
            if len(payload) != opened.st_size or (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ) != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
                raise FleetGlobalLockError("Fleet admission state changed during read")
            state = json.loads(payload.decode("utf-8"))
        except FleetGlobalLockError:
            raise
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FleetGlobalLockError("Fleet admission state is corrupt") from exc
        finally:
            if state_fd is not None:
                os.close(state_fd)
            if dir_fd is not None:
                os.close(dir_fd)
        return self._validate_state(state)

    def _write_state_fd(self, fd: int, state: dict[str, Any]) -> None:
        del fd  # The caller holds the canonical fleet lock while state is replaced.
        self._validate_state(state)
        payload = json.dumps(
            state,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if not payload or len(payload) > self._MAX_STATE_BYTES:
            raise FleetGlobalLockError("Fleet admission state serialization is invalid")
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        temp_name = f".{self._state_name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        temp_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        dir_fd: int | None = None
        temp_fd: int | None = None
        try:
            dir_fd = os.open(self._parent, directory_flags)
            parent = os.fstat(dir_fd)
            if (
                (parent.st_dev, parent.st_ino) != self._parent_identity
                or stat.S_IMODE(parent.st_mode) != 0o700
                or parent.st_uid != os.getuid()
            ):
                raise FleetGlobalLockError("Fleet admission state parent is unsafe")
            try:
                existing = os.stat(self._state_name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_uid != os.getuid()
                or stat.S_IMODE(existing.st_mode) != 0o600
            ):
                raise FleetGlobalLockError("Fleet admission state target is unsafe")
            temp_fd = os.open(temp_name, temp_flags, 0o600, dir_fd=dir_fd)
            os.fchmod(temp_fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise FleetGlobalLockError("Fleet admission state write was incomplete")
                view = view[written:]
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = None
            os.rename(
                temp_name,
                self._state_name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            os.fsync(dir_fd)
        except FleetGlobalLockError:
            raise
        except OSError as exc:
            raise FleetGlobalLockError("Fleet admission state could not be persisted") from exc
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if dir_fd is not None:
                try:
                    os.unlink(temp_name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(dir_fd)

    def has_unresolved_create_fences(self, fd: int) -> bool:
        return bool(self._read_state_fd(fd)["unresolved_create_fences"])

    def add_unresolved_create_fence(
        self,
        fd: int,
        *,
        operation_key: str,
        observed_current_count: int,
        profile_id: str,
        timestamp: int | None = None,
    ) -> None:
        digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
        state = self._read_state_fd(fd)
        fences = list(state["unresolved_create_fences"])
        if any(
            fence["operation_key_sha256"] == digest
            and fence["profile_id"] == profile_id
            for fence in fences
        ):
            return
        fences.append(
            {
                "operation_key_sha256": digest,
                "observed_current_count": observed_current_count,
                "profile_id": profile_id,
                "timestamp": int(time.time()) if timestamp is None else timestamp,
            }
        )
        self._write_state_fd(
            fd,
            {"version": 1, "unresolved_create_fences": fences},
        )

    def resolve_own_create_fence(
        self, fd: int, *, operation_key: str, profile_id: str
    ) -> bool:
        """Clear only the caller's exact pre-dispatch fence after proven success."""

        digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
        state = self._read_state_fd(fd)
        fences = list(state["unresolved_create_fences"])
        retained = [
            fence
            for fence in fences
            if not (
                fence["operation_key_sha256"] == digest
                and fence["profile_id"] == profile_id
            )
        ]
        if len(retained) == len(fences):
            return False
        self._write_state_fd(
            fd,
            {"version": 1, "unresolved_create_fences": retained},
        )
        return True

    def inspect(self) -> dict[str, Any]:
        """Return validated admission state without creating, writing, or clearing it."""

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.path, flags)
            fcntl.flock(fd, fcntl.LOCK_SH)
            state = self._read_state_fd(fd)
            return json.loads(json.dumps(state))
        except FleetGlobalLockError:
            raise
        except (OSError, ValueError) as exc:
            raise FleetGlobalLockError("Fleet admission state inspection failed") from exc
        finally:
            if "fd" in locals():
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


@dataclass(frozen=True)
class OperationReservation:
    dispatch: bool
    status: str
    result_id: str | None = None
    error_code: str | None = None


CREATE_TABLE_SQL = """
CREATE TABLE linear_mcp_operations (
    operation_key TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','success','failed','outcome_unknown')),
    result_id TEXT,
    error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK(
        (status = 'pending' AND result_id IS NULL AND error_code IS NULL) OR
        (status = 'success' AND result_id IS NOT NULL AND length(result_id) > 0
            AND error_code IS NULL) OR
        (status IN ('failed','outcome_unknown') AND result_id IS NULL
            AND error_code IS NOT NULL AND length(error_code) > 0)
    )
)
"""
MAX_LEDGER_BYTES = 64 * 1024 * 1024
ALLOWED_STATUSES = ("pending", "success", "failed", "outcome_unknown")


def _normalized_schema(sql: str) -> str:
    normalized = "".join(sql.lower().split())
    return normalized.replace("createtableifnotexists", "createtable", 1)


class OutboundLedger:
    """A serialized SQLite ledger that never opens the database by pathname.

    Cooperating processes serialize access with a profile-local flock. Database bytes
    are read through an openat/O_NOFOLLOW descriptor, deserialized into in-memory
    SQLite, then atomically persisted with openat + fsync + renameat.
    """

    def __init__(self, database_path: str, *, pending_timeout_seconds: int = 300) -> None:
        supplied_path = Path(database_path)
        if not supplied_path.is_absolute():
            raise OutboundLedgerError("Outbound ledger path must be absolute")
        if supplied_path.name in {"", ".", ".."}:
            raise OutboundLedgerError("Outbound ledger filename is invalid")
        try:
            canonical_parent = supplied_path.parent.resolve(strict=True)
            parent_stat = canonical_parent.stat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise OutboundLedgerError("Outbound ledger parent is unavailable") from exc
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.getuid()
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            raise OutboundLedgerError("Outbound ledger parent is not private to this user")

        self.path = canonical_parent / supplied_path.name
        self._name = supplied_path.name
        self._lock_name = f".{self._name}.lock"
        self.pending_timeout_seconds = int(pending_timeout_seconds)
        self._thread_lock = threading.RLock()
        self._closed = False
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            self._dir_fd = os.open(canonical_parent, directory_flags)
            opened_parent = os.fstat(self._dir_fd)
            if (
                (opened_parent.st_dev, opened_parent.st_ino)
                != (parent_stat.st_dev, parent_stat.st_ino)
                or not stat.S_ISDIR(opened_parent.st_mode)
                or opened_parent.st_uid != os.getuid()
                or stat.S_IMODE(opened_parent.st_mode) != 0o700
            ):
                raise OutboundLedgerError("Outbound ledger parent changed during open")
            lock_flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            self._lock_fd = os.open(
                self._lock_name,
                lock_flags,
                0o600,
                dir_fd=self._dir_fd,
            )
            os.fchmod(self._lock_fd, 0o600)
            lock_stat = os.fstat(self._lock_fd)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.getuid()
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
            ):
                raise OutboundLedgerError("Outbound ledger lock file is unsafe")
        except Exception:
            if hasattr(self, "_lock_fd"):
                os.close(self._lock_fd)
            if hasattr(self, "_dir_fd"):
                os.close(self._dir_fd)
            raise

        try:
            with self._exclusive_file_lock():
                if self._entry_exists():
                    db = self._load_database()
                else:
                    db = self._new_database()
                    self._persist_database(db)
                db.close()
        except Exception:
            self.close()
            raise

    @contextmanager
    def _exclusive_file_lock(self) -> Iterator[None]:
        with self._thread_lock:
            if self._closed:
                raise OutboundLedgerError("Outbound ledger is closed")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _entry_stat(self) -> os.stat_result:
        try:
            return os.stat(self._name, dir_fd=self._dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise OutboundLedgerError("Outbound ledger path could not be inspected") from exc

    def _entry_exists(self) -> bool:
        try:
            entry = self._entry_stat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(entry.st_mode):
            raise OutboundLedgerError("Outbound ledger path must not be a symlink")
        if not stat.S_ISREG(entry.st_mode):
            raise OutboundLedgerError("Outbound ledger path must be a regular file")
        return True

    @staticmethod
    def _validate_private_file(file_stat: os.stat_result) -> None:
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise OutboundLedgerError("Outbound ledger file is not private to this user")

    def _read_database_bytes(self) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self._name, flags, dir_fd=self._dir_fd)
        except OSError as exc:
            try:
                entry = self._entry_stat()
            except FileNotFoundError:
                raise OutboundLedgerError("Outbound ledger file disappeared") from exc
            if stat.S_ISLNK(entry.st_mode):
                raise OutboundLedgerError("Outbound ledger path must not be a symlink") from exc
            raise OutboundLedgerError("Outbound ledger file could not be opened safely") from exc
        try:
            opened = os.fstat(fd)
            self._validate_private_file(opened)
            path_stat = self._entry_stat()
            if (
                stat.S_ISLNK(path_stat.st_mode)
                or (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino)
                or opened.st_size <= 0
                or opened.st_size > MAX_LEDGER_BYTES
            ):
                raise OutboundLedgerError("Outbound ledger file identity is unsafe")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65_536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_LEDGER_BYTES:
                    raise OutboundLedgerError("Outbound ledger file is too large")
                chunks.append(chunk)
            final = os.fstat(fd)
            final_path = self._entry_stat()
            identity = (opened.st_dev, opened.st_ino)
            if (
                identity != (final.st_dev, final.st_ino)
                or identity != (final_path.st_dev, final_path.st_ino)
                or opened.st_mtime_ns != final.st_mtime_ns
                or opened.st_size != final.st_size
                or total != final.st_size
            ):
                raise OutboundLedgerError("Outbound ledger changed during secure read")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _new_database(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        try:
            db.execute(CREATE_TABLE_SQL)
            self._validate_schema(db)
            return db
        except Exception:
            db.close()
            raise

    def _load_database(self) -> sqlite3.Connection:
        payload = self._read_database_bytes()
        db = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        try:
            db.deserialize(payload)
            quick_check = db.execute("PRAGMA quick_check").fetchone()
            if quick_check != ("ok",):
                raise OutboundLedgerError("Outbound ledger integrity check failed")
            self._validate_schema(db)
            return db
        except Exception:
            db.close()
            raise

    def _persist_database(self, db: sqlite3.Connection) -> None:
        payload = db.serialize()
        if not payload or len(payload) > MAX_LEDGER_BYTES:
            raise OutboundLedgerError("Outbound ledger serialization was invalid")
        temp_name = f".{self._name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = -1
        try:
            fd = os.open(temp_name, flags, 0o600, dir_fd=self._dir_fd)
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OutboundLedgerError("Outbound ledger atomic write was incomplete")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(
                temp_name,
                self._name,
                src_dir_fd=self._dir_fd,
                dst_dir_fd=self._dir_fd,
            )
            os.fsync(self._dir_fd)
        except Exception as exc:
            if isinstance(exc, OutboundLedgerError):
                raise
            raise OutboundLedgerError("Outbound ledger could not be persisted safely") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name, dir_fd=self._dir_fd)
            except FileNotFoundError:
                pass

    def _validate_schema(self, db: sqlite3.Connection) -> None:
        expected_columns = [
            (0, "operation_key", "TEXT", 0, None, 1, 0),
            (1, "payload_hash", "TEXT", 1, None, 0, 0),
            (2, "tool_name", "TEXT", 1, None, 0, 0),
            (3, "profile_id", "TEXT", 1, None, 0, 0),
            (4, "actor_id", "TEXT", 1, None, 0, 0),
            (5, "team_id", "TEXT", 1, None, 0, 0),
            (6, "status", "TEXT", 1, None, 0, 0),
            (7, "result_id", "TEXT", 0, None, 0, 0),
            (8, "error_code", "TEXT", 0, None, 0, 0),
            (9, "created_at", "INTEGER", 1, None, 0, 0),
            (10, "updated_at", "INTEGER", 1, None, 0, 0),
        ]
        columns = [
            (
                int(row[0]),
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                row[4],
                int(row[5]),
                int(row[6]),
            )
            for row in db.execute("PRAGMA table_xinfo(linear_mcp_operations)")
        ]
        schema_row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='linear_mcp_operations'"
        ).fetchone()
        schema_exact = bool(
            schema_row
            and _normalized_schema(str(schema_row[0])) == _normalized_schema(CREATE_TABLE_SQL)
        )
        indexes = list(db.execute("PRAGMA index_list(linear_mcp_operations)"))
        index_exact = len(indexes) == 1
        if index_exact:
            index = indexes[0]
            index_exact = bool(
                int(index[2]) == 1 and str(index[3]) == "pk" and int(index[4]) == 0
            )
            if index_exact:
                index_rows = list(db.execute(f"PRAGMA index_xinfo('{index[1]}')"))
                key_rows = [row for row in index_rows if int(row[5]) == 1]
                index_exact = bool(
                    len(key_rows) == 1
                    and int(key_rows[0][1]) == 0
                    and str(key_rows[0][2]) == "operation_key"
                    and int(key_rows[0][3]) == 0
                    and str(key_rows[0][4]).upper() == "BINARY"
                )
        foreign_keys = list(db.execute("PRAGMA foreign_key_list(linear_mcp_operations)"))
        trigger_count = int(
            db.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='linear_mcp_operations'"
            ).fetchone()[0]
        )
        if (
            columns != expected_columns
            or not schema_exact
            or not index_exact
            or foreign_keys
            or trigger_count != 0
        ):
            raise OutboundLedgerError("Outbound ledger schema is incompatible")

        invalid_rows = int(
            db.execute(
                """
                SELECT count(*) FROM linear_mcp_operations WHERE NOT (
                    (status = 'pending' AND result_id IS NULL AND error_code IS NULL) OR
                    (status = 'success' AND result_id IS NOT NULL
                        AND length(result_id) > 0 AND error_code IS NULL) OR
                    (status IN ('failed','outcome_unknown') AND result_id IS NULL
                        AND error_code IS NOT NULL AND length(error_code) > 0)
                )
                """
            ).fetchone()[0]
        )
        if invalid_rows:
            raise OutboundLedgerError("Outbound ledger contains invalid status semantics")

        db.execute("SAVEPOINT linear_schema_probe")
        try:
            semantic_values = {
                "pending": (None, None),
                "success": ("probe-result", None),
                "failed": (None, "probe-failed"),
                "outcome_unknown": (None, "probe-unknown"),
            }
            for index, status_value in enumerate(ALLOWED_STATUSES):
                result_id, error_code = semantic_values[status_value]
                db.execute(
                    """
                    INSERT INTO linear_mcp_operations (
                        operation_key, payload_hash, tool_name, profile_id, actor_id,
                        team_id, status, result_id, error_code, created_at, updated_at
                    ) VALUES (?, ?, 'probe', 'probe', 'probe', 'probe', ?, ?, ?, 0, 0)
                    """,
                    (
                        f"schema-probe-{index}",
                        "0" * 64,
                        status_value,
                        result_id,
                        error_code,
                    ),
                )
            try:
                db.execute(
                    """
                    INSERT INTO linear_mcp_operations (
                        operation_key, payload_hash, tool_name, profile_id, actor_id,
                        team_id, status, created_at, updated_at
                    ) VALUES ('schema-probe-invalid', ?, 'probe', 'probe', 'probe',
                              'probe', 'invalid', 0, 0)
                    """,
                    ("0" * 64,),
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise OutboundLedgerError("Outbound ledger status constraint is incompatible")
        except sqlite3.DatabaseError as exc:
            raise OutboundLedgerError("Outbound ledger schema probe failed") from exc
        finally:
            db.execute("ROLLBACK TO linear_schema_probe")
            db.execute("RELEASE linear_schema_probe")

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _operation_hash(operation_key: str) -> str:
        return hashlib.sha256(operation_key.encode("utf-8")).hexdigest()

    def close(self) -> None:
        with self._thread_lock:
            if self._closed:
                return
            self._closed = True
            os.close(self._lock_fd)
            os.close(self._dir_fd)

    def reserve(
        self,
        *,
        operation_key: str,
        tool_name: str,
        payload: dict[str, Any],
        profile_id: str,
        actor_id: str,
        team_id: str,
    ) -> OperationReservation:
        if not operation_key or len(operation_key) > 200:
            raise OutboundLedgerError("operation_key is missing or too long")
        operation_id = self._operation_hash(operation_key)
        payload_hash = self._payload_hash(payload)
        now = int(time.time())
        with self._exclusive_file_lock():
            db = self._load_database()
            changed = False
            try:
                row = db.execute(
                    """
                    SELECT payload_hash, tool_name, profile_id, actor_id, team_id,
                           status, result_id, error_code, updated_at
                    FROM linear_mcp_operations WHERE operation_key = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if row is None:
                    db.execute(
                        """
                        INSERT INTO linear_mcp_operations (
                            operation_key, payload_hash, tool_name, profile_id,
                            actor_id, team_id, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            operation_id,
                            payload_hash,
                            tool_name,
                            profile_id,
                            actor_id,
                            team_id,
                            now,
                            now,
                        ),
                    )
                    changed = True
                    reservation = OperationReservation(True, "pending")
                else:
                    identity = row[:5]
                    expected = (payload_hash, tool_name, profile_id, actor_id, team_id)
                    if identity != expected:
                        raise OutboundLedgerError(
                            "operation_key already exists with a different payload or identity"
                        )
                    status_value, result_id, error_code, updated_at = row[5:]
                    if (
                        status_value == "pending"
                        and now - int(updated_at) > self.pending_timeout_seconds
                    ):
                        status_value = "outcome_unknown"
                        error_code = "stale_pending"
                        db.execute(
                            """
                            UPDATE linear_mcp_operations
                            SET status='outcome_unknown', error_code=?, updated_at=?
                            WHERE operation_key=?
                            """,
                            (error_code, now, operation_id),
                        )
                        changed = True
                    reservation = OperationReservation(
                        False,
                        str(status_value),
                        result_id,
                        error_code,
                    )
                if changed:
                    self._persist_database(db)
                return reservation
            finally:
                db.close()

    def lookup(
        self,
        *,
        operation_key: str,
        tool_name: str,
        payload: dict[str, Any],
        profile_id: str,
        actor_id: str,
        team_id: str,
    ) -> OperationReservation | None:
        """Return an existing exact operation without creating or mutating a row."""
        if not operation_key or len(operation_key) > 200:
            raise OutboundLedgerError("operation_key is missing or too long")
        operation_id = self._operation_hash(operation_key)
        payload_hash = self._payload_hash(payload)
        with self._exclusive_file_lock():
            db = self._load_database()
            try:
                row = db.execute(
                    """
                    SELECT payload_hash, tool_name, profile_id, actor_id, team_id,
                           status, result_id, error_code
                    FROM linear_mcp_operations WHERE operation_key = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if row is None:
                    return None
                identity = row[:5]
                expected = (payload_hash, tool_name, profile_id, actor_id, team_id)
                if identity != expected:
                    raise OutboundLedgerError(
                        "operation_key already exists with a different payload or identity"
                    )
                status_value, result_id, error_code = row[5:]
                return OperationReservation(
                    False,
                    str(status_value),
                    result_id,
                    error_code,
                )
            finally:
                db.close()

    def mark_success(self, operation_key: str, *, result_id: str | None = None) -> None:
        if not isinstance(result_id, str) or not result_id:
            raise OutboundLedgerError("result_id is required for successful operations")
        self._mark(operation_key, "success", result_id=result_id, error_code=None)

    def mark_unknown(self, operation_key: str, *, error_code: str) -> None:
        if not isinstance(error_code, str) or not error_code:
            raise OutboundLedgerError("error_code is required for unknown operations")
        self._mark(operation_key, "outcome_unknown", result_id=None, error_code=error_code)

    def mark_failed(self, operation_key: str, *, error_code: str) -> None:
        if not isinstance(error_code, str) or not error_code:
            raise OutboundLedgerError("error_code is required for failed operations")
        self._mark(operation_key, "failed", result_id=None, error_code=error_code)

    def _mark(
        self,
        operation_key: str,
        status_value: str,
        *,
        result_id: str | None,
        error_code: str | None,
    ) -> None:
        operation_id = self._operation_hash(operation_key)
        with self._exclusive_file_lock():
            db = self._load_database()
            try:
                cursor = db.execute(
                    """
                    UPDATE linear_mcp_operations
                    SET status=?, result_id=?, error_code=?, updated_at=?
                    WHERE operation_key=? AND status='pending'
                    """,
                    (status_value, result_id, error_code, int(time.time()), operation_id),
                )
                if cursor.rowcount != 1:
                    raise OutboundLedgerError("operation is missing or no longer pending")
                self._persist_database(db)
            finally:
                db.close()
