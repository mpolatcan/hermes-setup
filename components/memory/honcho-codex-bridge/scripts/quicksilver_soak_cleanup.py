#!/usr/bin/env python3
"""Delete Quicksilver recovery checkouts only after the real Honcho soak gate passes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

HOME = Path("/Users/mutlupolatcan")
JOBS = HOME / ".hermes/profiles/general/cron/jobs.json"
HISTORY = HOME / ".hermes/profiles/general/metrics/honcho-codex-soak.jsonl"
MARKER = HOME / ".hermes/profiles/general/metrics/quicksilver-soak-cleanup.json"
CANONICAL = HOME / "Desktop/hermes-setup"
RECOVERY = HOME / "Developer/hermes-setup"
CANDIDATE = HOME / ".hermes/runtime/hermes-agent-candidate-v2026.7.20"
PRODUCTION_RUNTIME = HOME / ".hermes/runtime/hermes-agent"
SOAK_JOB_ID = "7784a37d506f"
EXPECTED_CANDIDATE_HEAD = "3ef6bbd201263d354fd83ec55b3c306ded2eb72a"
OFFICIAL_HERMES_ORIGIN = "https://github.com/NousResearch/hermes-agent.git"
TARGETS = (RECOVERY, CANDIDATE)


class GateError(RuntimeError):
    pass


def run(command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def git(path: Path, *args: str) -> str:
    result = run(["/usr/bin/git", "-C", str(path), *args])
    if result.returncode != 0:
        raise GateError(f"git check failed for {path.name}: {' '.join(args)}")
    return result.stdout.strip()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"invalid state file: {path.name}") from exc


def soak_gate() -> dict[str, Any]:
    jobs = load_json(JOBS)
    job = next((item for item in jobs.get("jobs", []) if item.get("id") == SOAK_JOB_ID), None)
    if not isinstance(job, dict):
        raise GateError("soak job missing")
    completed = int((job.get("repeat") or {}).get("completed") or 0)
    if completed < 8:
        raise GateError(f"soak job incomplete: {completed}/8")

    try:
        records = [json.loads(line) for line in HISTORY.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError("soak history unavailable") from exc

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        timestamp = record.get("timestamp")
        if not isinstance(timestamp, str):
            raise GateError("soak record has no timestamp")
        try:
            day = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
        except ValueError as exc:
            raise GateError("soak record timestamp invalid") from exc
        by_day[day].append(record)

    days = sorted(by_day)
    if len(days) < 7:
        raise GateError(f"distinct soak days incomplete: {len(days)}/7")
    considered = days[-7:]
    if any(
        not record.get("ok") or record.get("failures")
        for day in considered
        for record in by_day[day]
    ):
        raise GateError("one or more soak records failed")
    return {"completed": completed, "days": considered, "records": sum(len(by_day[d]) for d in considered)}


def checkout_gate() -> dict[str, Any]:
    for path in (CANONICAL, *TARGETS):
        if not path.exists() or path.is_symlink() or not (path / ".git").exists():
            raise GateError(f"checkout path invalid: {path}")
        if git(path, "status", "--porcelain"):
            raise GateError(f"checkout is dirty: {path}")

    if git(CANONICAL, "branch", "--show-current") != "main":
        raise GateError("canonical checkout is not on main")
    canonical_head = git(CANONICAL, "rev-parse", "HEAD")
    upstream_head = git(CANONICAL, "rev-parse", "@{upstream}")
    if canonical_head != upstream_head:
        raise GateError("canonical checkout is not aligned with upstream")

    recovery_head = git(RECOVERY, "rev-parse", "HEAD")
    if git(RECOVERY, "remote", "get-url", "origin") != git(CANONICAL, "remote", "get-url", "origin"):
        raise GateError("recovery and canonical remotes differ")
    ancestor = run([
        "/usr/bin/git", "-C", str(CANONICAL), "merge-base", "--is-ancestor", recovery_head, canonical_head
    ])
    if ancestor.returncode != 0:
        raise GateError("recovery commit is not a canonical ancestor")

    candidate_head = git(CANDIDATE, "rev-parse", "HEAD")
    if candidate_head != EXPECTED_CANDIDATE_HEAD:
        raise GateError("managed candidate HEAD changed")
    if git(PRODUCTION_RUNTIME, "remote", "get-url", "origin") != OFFICIAL_HERMES_ORIGIN:
        raise GateError("production runtime origin is not the official upstream")
    if Path(os.path.realpath(PRODUCTION_RUNTIME)) in TARGETS:
        raise GateError("production runtime resolves to a deletion target")

    return {
        "canonical_head": canonical_head,
        "recovery_head": recovery_head,
        "candidate_head": candidate_head,
    }


def usage_gate() -> None:
    processes = run(["/bin/ps", "-axo", "command="])
    if processes.returncode != 0:
        raise GateError("process inventory failed")
    if any(str(target) in processes.stdout for target in TARGETS):
        raise GateError("a running process references a deletion target")

    lsof = shutil.which("lsof")
    if not lsof:
        raise GateError("lsof unavailable")
    for target in TARGETS:
        result = run([lsof, "+D", str(target)], timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            raise GateError(f"open files under deletion target: {target}")
        if result.returncode not in {0, 1}:
            raise GateError(f"lsof inspection failed: {target}")

    active_files: list[Path] = []
    active_files.extend((HOME / ".hermes/scripts").glob("*"))
    active_files.extend((HOME / "Library/LaunchAgents").glob("ai.hermes*.plist"))
    for profile in ("general", "assistant", "researcher", "marketing", "coder", "writer", "producer", "finance", "health"):
        root = HOME / f".hermes/profiles/{profile}"
        active_files.extend((root / "cron").glob("*.json"))
        active_files.extend((root / "scripts").glob("*"))
        active_files.extend((root / "config.yaml", root / "init.sh"))

    self_path = Path(__file__).resolve()
    for path in active_files:
        if path.resolve() == self_path or not path.is_file():
            continue
        if any(token in path.name for token in (".pre-", ".bak-")) or path.suffix == ".log":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            raise GateError(f"active reference scan failed: {path}")
        if any(str(target) in text for target in TARGETS):
            raise GateError(f"active file references a deletion target: {path}")


def evaluate() -> dict[str, Any]:
    if MARKER.exists():
        return {"ready": False, "completed": True, "reason": "cleanup already completed"}
    try:
        soak = soak_gate()
        checkouts = checkout_gate()
        usage_gate()
    except GateError as exc:
        return {"ready": False, "completed": False, "reason": str(exc)}
    return {"ready": True, "completed": False, "soak": soak, "checkouts": checkouts}


def perform(state: dict[str, Any]) -> None:
    if not state.get("ready"):
        return
    for target in TARGETS:
        shutil.rmtree(target)
    if any(target.exists() for target in TARGETS):
        raise GateError("cleanup verification failed")
    marker = {
        "completed_at": datetime.now().astimezone().isoformat(),
        "deleted": [str(target) for target in TARGETS],
        "soak": state["soak"],
        "checkouts": state["checkouts"],
    }
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "## Quicksilver soak cleanup\n\n"
        "| Gate | Sonuç |\n|---|---:|\n"
        f"| Soak | {state['soak']['completed']}/8 · 7 farklı gün · 0 hata |\n"
        "| Developer recovery | Silindi |\n"
        "| Managed candidate | Silindi |\n"
        "| Homebrew 0.18.2 rollback | Korundu |"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="print gate state without deleting")
    args = parser.parse_args()
    state = evaluate()
    if args.check:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0
    try:
        perform(state)
    except (OSError, GateError) as exc:
        print(f"Quicksilver cleanup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
