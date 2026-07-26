#!/usr/bin/env python3
"""Probe authenticated Honcho queue totals without exporting JWT material."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

DEFAULT_DOCKER = "/Users/mutlupolatcan/.orbstack/bin/docker"
DEFAULT_WORKSPACES = ("polatcan-gaming", "polatcan-finance", "polatcan-health")
INNER_PROBE = r'''
import json,sys,urllib.request
from src.security import JWTParams,create_jwt
workspace=sys.argv[1]
token=create_jwt(JWTParams(w=workspace))
request=urllib.request.Request(
    f"http://127.0.0.1:8000/v3/workspaces/{workspace}/queue/status",
    headers={"Authorization": "Bearer " + token},
)
with urllib.request.urlopen(request,timeout=10) as response:
    if response.status != 200:
        raise SystemExit(1)
    print(json.dumps(json.loads(response.read())))
'''


def evaluate(
    rows: dict[str, dict[str, Any]],
    *,
    pending_threshold: int,
    in_progress_threshold: int,
) -> dict[str, Any]:
    pending = sum(int(row.get("pending_work_units", 0)) for row in rows.values())
    in_progress = sum(int(row.get("in_progress_work_units", 0)) for row in rows.values())
    exceeded: list[str] = []
    if pending > pending_threshold:
        exceeded.append(f"pending>{pending_threshold}")
    if in_progress > in_progress_threshold:
        exceeded.append(f"in_progress>{in_progress_threshold}")
    return {
        "ok": not exceeded,
        "pending": pending,
        "in_progress": in_progress,
        "threshold": ",".join(exceeded),
        "workspaces": len(rows),
    }


def collect(workspaces: list[str], docker: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for workspace in workspaces:
        completed = subprocess.run(
            [
                docker,
                "exec",
                "server-api-1",
                "/app/.venv/bin/python",
                "-c",
                INNER_PROBE,
                workspace,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"queue probe failed for {workspace}")
        rows[workspace] = json.loads(completed.stdout)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", action="append", dest="workspaces")
    parser.add_argument("--pending-threshold", type=int, default=25)
    parser.add_argument("--in-progress-threshold", type=int, default=10)
    args = parser.parse_args(argv)
    workspaces: list[str] = (
        [str(item) for item in args.workspaces]
        if args.workspaces
        else list(DEFAULT_WORKSPACES)
    )
    docker = os.environ.get("HERMES_WATCHDOG_DOCKER", DEFAULT_DOCKER)
    try:
        rows = collect(workspaces, docker)
        result = evaluate(
            rows,
            pending_threshold=args.pending_threshold,
            in_progress_threshold=args.in_progress_threshold,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError):
        print(json.dumps({"ok": False, "error": "queue_probe_failed"}))
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
