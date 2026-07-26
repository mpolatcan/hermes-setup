#!/usr/bin/env python3
"""Delete only the approved legacy Honcho workspaces, safely and audibly."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "http://127.0.0.1:8000"
ACTIVE_WORKSPACES = {"polatcan-gaming", "polatcan-finance", "polatcan-health"}
LEGACY_WORKSPACES = {
    "hermes_general",
    "hermes_researcher",
    "hermes_assistant",
    "hermes_marketing",
    "hermes_coder",
    "hermes_writer",
    "hermes_producer",
    "hermes_finance",
    "hermes_health",
}
CONFIRMATION = "DELETE-65-LEGACY-SESSIONS-AND-9-WORKSPACES"


def request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Honcho API {method} {path} returned {exc.code}: {body[:300]}") from exc


def page_items(path: str) -> list[dict[str, Any]]:
    status, body = request_json("POST", f"{path}?page=1&size=100", {})
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise RuntimeError(f"unexpected list response for {path}")
    if int(body.get("pages", 1)) > 1:
        raise RuntimeError(f"pagination exceeds safety limit for {path}")
    return body["items"]


def resource_ids(items: list[dict[str, Any]]) -> list[str]:
    try:
        return [str(item["id"]) for item in items]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("resource list item is missing id") from exc


def workspace_names() -> set[str]:
    return set(resource_ids(page_items("/v3/workspaces/list")))


def session_names(workspace: str) -> list[str]:
    encoded = urllib.parse.quote(workspace, safe="")
    return resource_ids(page_items(f"/v3/workspaces/{encoded}/sessions/list"))


def validate_inventory(actual: set[str]) -> None:
    expected = ACTIVE_WORKSPACES | LEGACY_WORKSPACES
    if actual != expected:
        raise RuntimeError(
            "unexpected workspace inventory: "
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )


def validate_deletion_target(workspace: str) -> None:
    if workspace in ACTIVE_WORKSPACES:
        raise RuntimeError(f"protected workspace cannot be deleted: {workspace}")
    if workspace not in LEGACY_WORKSPACES:
        raise RuntimeError(f"workspace is not allowlisted: {workspace}")


def require_confirmation(value: str | None) -> None:
    if value != CONFIRMATION:
        raise SystemExit(f"--confirm must equal {CONFIRMATION}")


def wait_until(predicate, description: str, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(1)
    raise RuntimeError(f"timeout waiting for {description}")


def delete_workspace(workspace: str) -> dict[str, int | str]:
    validate_deletion_target(workspace)
    encoded_workspace = urllib.parse.quote(workspace, safe="")
    sessions = session_names(workspace)
    for session in sessions:
        encoded_session = urllib.parse.quote(session, safe="")
        status, _ = request_json(
            "DELETE",
            f"/v3/workspaces/{encoded_workspace}/sessions/{encoded_session}",
        )
        if status not in (202, 204):
            raise RuntimeError(f"unexpected session delete status: {status}")
    wait_until(lambda: not session_names(workspace), f"sessions to be deleted from {workspace}")
    status, _ = request_json("DELETE", f"/v3/workspaces/{encoded_workspace}")
    if status != 202:
        raise RuntimeError(f"unexpected workspace delete status: {status}")
    wait_until(lambda: workspace not in workspace_names(), f"workspace {workspace} to be deleted")
    return {"workspace": workspace, "sessions_deleted": len(sessions)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)

    initial = workspace_names()
    validate_inventory(initial)
    inventory = {workspace: len(session_names(workspace)) for workspace in sorted(LEGACY_WORKSPACES)}
    report: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "active": sorted(ACTIVE_WORKSPACES),
        "legacy_sessions": inventory,
        "legacy_session_total": sum(inventory.values()),
    }
    if not args.apply:
        print(json.dumps(report, sort_keys=True))
        return 0

    require_confirmation(args.confirm)
    if report["legacy_session_total"] != 65:
        raise RuntimeError(f"expected exactly 65 legacy sessions, got {report['legacy_session_total']}")
    results = [delete_workspace(workspace) for workspace in sorted(LEGACY_WORKSPACES)]
    final = workspace_names()
    if final != ACTIVE_WORKSPACES:
        raise RuntimeError(f"unexpected final workspace inventory: {sorted(final)}")
    report["results"] = results
    report["final"] = sorted(final)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
