#!/usr/bin/env python3
"""Narrow request facade and external coordinator service."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from restart_coordinator import (
    Coordinator,
    CoordinatorStore,
    ProcessRuntime,
    RequestError,
    requester_from_ancestry,
    requester_from_home,
)

DEFAULT_STATE = Path("/Users/mutlupolatcan/.hermes/restart-coordinator")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hermes-restart-coordinator")
    root.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = root.add_subparsers(dest="command", required=True)
    request = commands.add_parser("request", help="enqueue a validated JSON request")
    request.add_argument("json_file", type=Path)
    commands.add_parser("run", help="run the external coordinator loop").add_argument("--once", action="store_true")
    commands.add_parser("status", help="show queue integrity and outbox counts")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    store = CoordinatorStore(
        state_dir / "queue.sqlite3",
        allowed_artifact_roots=[
            "/Users/mutlupolatcan/.hermes/profiles",
            "/Users/mutlupolatcan/.hermes/runtime",
            "/Users/mutlupolatcan/.hermes/backups",
        ],
    )

    if args.command == "request":
        try:
            home_requester = requester_from_home(os.environ.get("HERMES_HOME", ""))
            runtime = ProcessRuntime()
            ancestor_requester = requester_from_ancestry(runtime.ancestry(), runtime.gateway_pids())
            if home_requester != ancestor_requester:
                raise RequestError("requester_identity_mismatch")
            requester = home_requester
            payload = json.loads(args.json_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise RequestError("request_must_be_object")
            print(json.dumps(store.enqueue(requester, payload), sort_keys=True))
            return 0
        except (OSError, json.JSONDecodeError, RequestError) as exc:
            print(json.dumps({"status": "rejected", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
            return 2

    if args.command == "status":
        print(json.dumps({"integrity": store.integrity(), "outbox": store.outbox_counts()}, sort_keys=True))
        return 0

    lock_path = state_dir / "coordinator.lock"
    lock = lock_path.open("a+", encoding="utf-8")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"status": "already_running"}, sort_keys=True), file=sys.stderr)
        return 3

    coordinator = Coordinator(store, ProcessRuntime())
    while True:
        try:
            result = coordinator.process_once()
            for event in store.pending_outbox():
                print(json.dumps({"event": "restart_terminal", **event["payload"]}, sort_keys=True), flush=True)
                store.ack_outbox(event["id"])
            if result is not None:
                print(json.dumps({"event": "restart_execution", "task_id": result["task_id"], "status": result["status"]}, sort_keys=True), flush=True)
        except Exception as exc:  # launchd keeps the service alive; state machine recovers on the next tick
            print(json.dumps({"event": "coordinator_error", "type": type(exc).__name__, "message": str(exc)}, sort_keys=True), file=sys.stderr, flush=True)
        if args.once:
            return 0
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
