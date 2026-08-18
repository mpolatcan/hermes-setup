"""Deterministic, read-only Linear Operations issue-quota watchdog."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

try:
    from .linear_client import (
        LINEAR_ISSUE_CAPACITY,
        LINEAR_ISSUE_CRITICAL_THRESHOLD,
        LinearClient,
        count_operations_issues,
    )
except ImportError:  # Direct module loading in tests and profile-local scripts.
    from linear_client import (
        LINEAR_ISSUE_CAPACITY,
        LINEAR_ISSUE_CRITICAL_THRESHOLD,
        LinearClient,
        count_operations_issues,
    )


CAPACITY = LINEAR_ISSUE_CAPACITY
WARNING_THRESHOLD = 200
HIGH_THRESHOLD = 225
CRITICAL_THRESHOLD = LINEAR_ISSUE_CRITICAL_THRESHOLD
MATERIAL_TOTAL_CHANGE = 5
MEANINGFUL_EXHAUSTION_MOVE_DAYS = 7
ROLLING_SAMPLE_LIMIT = 7
STATE_SCHEMA = "linear-operations-quota-watchdog/v1"
DRY_RUN_SCHEMA = "linear-operations-quota-watchdog-dry-run/v1"
STATE_FILENAME = "linear-operations-quota-watchdog.json"


@dataclass(frozen=True)
class Evaluation:
    alert: str
    summary: Mapping[str, Any]
    next_state: Mapping[str, Any]


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("continuity state is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("continuity state is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("continuity state is invalid")
    return parsed.astimezone(timezone.utc)


def _severity(total: int) -> str:
    if total >= CRITICAL_THRESHOLD:
        return "critical"
    if total >= HIGH_THRESHOLD:
        return "high"
    if total >= WARNING_THRESHOLD:
        return "warning"
    return "ok"


def _validate_directory(path: Path) -> Path:
    try:
        supplied_stat = path.lstat()
        canonical = path.resolve(strict=True)
        canonical_stat = canonical.stat()
    except OSError as exc:
        raise ValueError("state directory is unavailable or unsafe") from exc
    if (
        stat.S_ISLNK(supplied_stat.st_mode)
        or not stat.S_ISDIR(canonical_stat.st_mode)
        or canonical_stat.st_uid != os.getuid()
        or stat.S_IMODE(canonical_stat.st_mode) != 0o700
    ):
        raise ValueError("state directory must be an owned 0700 directory")
    return canonical


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "samples", "last_alert"}:
        raise ValueError("continuity state is invalid")
    if value.get("schema") != STATE_SCHEMA or not isinstance(
        value.get("samples"), list
    ):
        raise ValueError("continuity state is invalid")
    samples = value["samples"]
    if not 1 <= len(samples) <= ROLLING_SAMPLE_LIMIT:
        raise ValueError("continuity state is invalid")
    previous_at: datetime | None = None
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {"at", "total"}:
            raise ValueError("continuity state is invalid")
        at = _parse_timestamp(sample["at"])
        total = sample["total"]
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ValueError("continuity state is invalid")
        if previous_at is not None and at <= previous_at:
            raise ValueError("continuity state is invalid")
        previous_at = at
    last_alert = value["last_alert"]
    if last_alert is not None:
        required = {"total", "severity", "growth_trend", "estimated_exhaustion_date"}
        if not isinstance(last_alert, dict) or set(last_alert) != required:
            raise ValueError("continuity state is invalid")
        if (
            isinstance(last_alert["total"], bool)
            or not isinstance(last_alert["total"], int)
            or last_alert["total"] < 0
            or last_alert["severity"] not in {"ok", "warning", "high", "critical"}
            or last_alert["growth_trend"] not in {"unknown", "growing", "nonpositive"}
        ):
            raise ValueError("continuity state is invalid")
        exhaustion = last_alert["estimated_exhaustion_date"]
        if exhaustion is not None:
            if not isinstance(exhaustion, str):
                raise ValueError("continuity state is invalid")
            try:
                datetime.strptime(exhaustion, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("continuity state is invalid") from exc
    return value


class QuotaWatchdog:
    """Evaluate and persist secret-free quota continuity in one explicit directory."""

    def __init__(self, state_dir: str | Path, *, clock: Callable[[], datetime]) -> None:
        self._supplied_state_dir = Path(state_dir)
        self.clock = clock

    def _state_path(self) -> Path:
        return _validate_directory(self._supplied_state_dir) / STATE_FILENAME

    @contextmanager
    def locked(self) -> Iterator[None]:
        directory = _validate_directory(self._supplied_state_dir)
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(directory, flags)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> dict[str, Any] | None:
        path = self._state_path()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError("continuity state is unavailable or unsafe") from exc
        try:
            file_stat = os.fstat(fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.getuid()
                or stat.S_IMODE(file_stat.st_mode) != 0o600
            ):
                raise ValueError("continuity state is unavailable or unsafe")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                value = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError("continuity state is invalid") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        return _validate_state(value)

    def evaluate(self, total: int) -> Evaluation:
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ValueError("issue total must be a non-negative integer")
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        previous = self._load()
        samples = list(previous["samples"]) if previous else []
        current_sample = {"at": _utc_timestamp(now), "total": total}
        if samples and _parse_timestamp(samples[-1]["at"]) == now:
            samples[-1] = current_sample
        else:
            if samples and _parse_timestamp(samples[-1]["at"]) > now:
                raise ValueError("continuity state is newer than the current clock")
            samples.append(current_sample)
        samples = samples[-ROLLING_SAMPLE_LIMIT:]

        growth: float | None = None
        if len(samples) >= 2:
            oldest_at = _parse_timestamp(samples[0]["at"])
            newest_at = _parse_timestamp(samples[-1]["at"])
            elapsed_days = (newest_at - oldest_at).total_seconds() / 86400
            if elapsed_days > 0:
                growth = (samples[-1]["total"] - samples[0]["total"]) / elapsed_days
                if not math.isfinite(growth):
                    raise ValueError("continuity state produced invalid growth")
        trend = (
            "unknown"
            if growth is None
            else ("growing" if growth > 0 else "nonpositive")
        )
        buffer = CAPACITY - total
        exhaustion: str | None = None
        if growth is not None and growth > 0:
            days_remaining = max(buffer, 0) / growth
            exhaustion = (now + timedelta(days=days_remaining)).date().isoformat()

        severity = _severity(total)
        previous_alert = previous["last_alert"] if previous else None
        reasons: list[str] = []
        if previous_alert is None:
            if severity != "ok":
                reasons.append("first_alert")
        else:
            if severity != previous_alert["severity"]:
                reasons.append("severity_changed")
            if abs(total - previous_alert["total"]) >= MATERIAL_TOTAL_CHANGE:
                reasons.append("total_changed_materially")
            initial_zero_confirmation = (
                previous_alert["growth_trend"] == "unknown"
                and trend == "nonpositive"
                and growth == 0
                and total == previous_alert["total"]
            )
            if (
                trend != previous_alert["growth_trend"]
                and not initial_zero_confirmation
            ):
                reasons.append("trend_changed")
            old_exhaustion = previous_alert["estimated_exhaustion_date"]
            if (old_exhaustion is None) != (exhaustion is None):
                reasons.append("exhaustion_moved_meaningfully")
            elif old_exhaustion is not None and exhaustion is not None:
                old_date = datetime.strptime(old_exhaustion, "%Y-%m-%d").date()
                new_date = datetime.strptime(exhaustion, "%Y-%m-%d").date()
                if abs((new_date - old_date).days) >= MEANINGFUL_EXHAUSTION_MOVE_DAYS:
                    reasons.append("exhaustion_moved_meaningfully")

        alert = ""
        if reasons:
            growth_text = "unavailable" if growth is None else f"{growth:g} issues/day"
            exhaustion_text = exhaustion or "unavailable"
            alert = (
                f"Linear Operations quota {severity.upper()}: {total}/{CAPACITY} issues "
                f"({buffer} remaining). Rolling net growth: {growth_text}. "
                f"Estimated exhaustion: {exhaustion_text}."
            )
        alert_state = previous_alert
        if alert:
            alert_state = {
                "total": total,
                "severity": severity,
                "growth_trend": trend,
                "estimated_exhaustion_date": exhaustion,
            }
        summary = {
            "as_of": _utc_timestamp(now),
            "total": total,
            "capacity": CAPACITY,
            "buffer": buffer,
            "severity": severity,
            "rolling_net_growth_per_day": None if growth is None else round(growth, 6),
            "growth_trend": trend,
            "estimated_exhaustion_date": exhaustion,
            "notification_reasons": reasons,
        }
        next_state = {
            "schema": STATE_SCHEMA,
            "samples": samples,
            "last_alert": alert_state,
        }
        return Evaluation(alert=alert, summary=summary, next_state=next_state)

    def save(self, value: Mapping[str, Any]) -> None:
        validated = _validate_state(dict(value))
        path = self._state_path()
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.getuid()
            or stat.S_IMODE(existing.st_mode) != 0o600
        ):
            raise ValueError("continuity state is unavailable or unsafe")
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        encoded = json.dumps(validated, sort_keys=True, separators=(",", ":")) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


async def _run(
    args: argparse.Namespace,
    *,
    client_factory: Callable[..., LinearClient],
    clock: Callable[[], datetime],
    emit_alert: Callable[[str], None],
) -> Evaluation:
    watchdog = QuotaWatchdog(args.state_dir, clock=clock)
    client = client_factory(oauth_file=args.oauth_file)
    with watchdog.locked():
        try:
            await client.connect()
            total = await count_operations_issues(
                client, args.team_id, args.expected_team_key
            )
        finally:
            await client.close()
        result = watchdog.evaluate(total)
        if not args.dry_run:
            if result.alert:
                emit_alert(result.alert)
            watchdog.save(result.next_state)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Linear Operations quota watchdog"
    )
    parser.add_argument("--oauth-file", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--expected-team-key", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[..., LinearClient] = LinearClient,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    args = _parser().parse_args(argv)

    def emit_alert(body: str) -> None:
        sys.stdout.write(body + "\n")
        sys.stdout.flush()

    try:
        result = asyncio.run(
            _run(
                args,
                client_factory=client_factory,
                clock=clock,
                emit_alert=emit_alert,
            )
        )
    except Exception:
        print("Linear quota watchdog failed safely.", file=sys.stderr)
        return 1
    if args.dry_run:
        summary = {
            "schema": DRY_RUN_SCHEMA,
            **result.summary,
            "alert": result.alert,
            "would_alert": bool(result.alert),
            "would_write_state": False,
        }
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
