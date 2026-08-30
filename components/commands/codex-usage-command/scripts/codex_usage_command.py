#!/usr/bin/env python3
"""Read official Codex ChatGPT usage through app-server JSON-RPC.

Uses `account/rateLimits/read`; never scrapes the TUI, calls `/usage`
directly, consumes reset credits, or prints account identifiers/tokens.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

DASHBOARD = "https://chatgpt.com/codex/settings/usage"


def _hermes_root() -> Path:
    # Installed layout: ~/.hermes/plugins/codex-usage/scripts/this_file.py
    resolved = Path(__file__).resolve()
    for parent in resolved.parents:
        if parent.name == ".hermes":
            return parent
    return Path.home() / ".hermes"


def _codex_binary() -> str:
    explicit = os.environ.get("CODEX_BIN")
    if explicit:
        return explicit
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    real_home = _hermes_root().parent
    for candidate in (real_home / ".local/bin/codex", Path("/opt/homebrew/bin/codex")):
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("Codex CLI not found; set CODEX_BIN or add codex to PATH")


def _codex_home() -> str:
    explicit = os.environ.get("CODEX_HOME")
    if explicit:
        return explicit
    return str(_hermes_root().parent / ".codex")


def _fmt_until(ts: int | float | None) -> str:
    if not ts:
        return "?"
    try:
        seconds = max(0, int(ts) - int(time.time()))
    except Exception:
        return "?"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _window_label(limit: dict) -> str:
    minutes = (limit or {}).get("windowDurationMins")
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return "Usage"
    minutes = int(minutes)
    if minutes % 1440 == 0:
        return f"{minutes // 1440}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute"


def _fmt_percent(limit: dict) -> str:
    value = (limit or {}).get("usedPercent")
    return f"%{value}" if value is not None else "?"


def _read_json_lines(proc: subprocess.Popen, want_id: int, timeout: float) -> dict:
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 8192).decode("utf-8", "replace")
        except BlockingIOError:
            continue
        if not chunk:
            continue
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("id") == want_id:
                if "error" in obj:
                    raise RuntimeError(json.dumps(obj["error"], ensure_ascii=False))
                return obj.get("result") or {}
    raise TimeoutError(f"Codex app-server response timeout for id={want_id}")


def _codex_rpc_rate_limits() -> dict:
    proc = subprocess.Popen(
        [_codex_binary(), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(_hermes_root().parent),
        env={**os.environ, "CODEX_HOME": _codex_home()},
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {"name": "hermes", "title": "Hermes", "version": "1"},
                "capabilities": {"experimentalApi": True, "requestAttestation": False},
            },
        }, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        _read_json_lines(proc, 1, timeout=10)
        proc.stdin.write(json.dumps({
            "method": "account/rateLimits/read", "id": 2, "params": None,
        }, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        return _read_json_lines(proc, 2, timeout=20)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def render_usage(data: dict) -> str:
    rate = data.get("rateLimits") or {}
    primary = rate.get("primary") or {}
    secondary = rate.get("secondary") or {}
    reset_credits = data.get("rateLimitResetCredits") or {}
    lines = [
        "## Codex Usage — Official",
        "",
        "| Field | Value |",
        "|---|---:|",
        f"| Plan | `{rate.get('planType') or '?'}` |",
        f"| Limit status | {'reached' if rate.get('rateLimitReachedType') else 'available'} |",
        f"| {_window_label(primary)} usage | {_fmt_percent(primary)} |",
        f"| {_window_label(primary)} reset | {_fmt_until(primary.get('resetsAt'))} |",
    ]
    if secondary:
        lines.extend([
            f"| {_window_label(secondary)} usage | {_fmt_percent(secondary)} |",
            f"| {_window_label(secondary)} reset | {_fmt_until(secondary.get('resetsAt'))} |",
        ])
    lines.append(f"| Reset credits | {reset_credits.get('availableCount', 0)} |")

    by_id = data.get("rateLimitsByLimitId") or {}
    extras = [(key, item) for key, item in by_id.items() if key != "codex"]
    if extras:
        lines.extend(["", "## Additional limits", "", "| Limit | Window | Usage | Reset |", "|---|---|---:|---|"])
        for key, item in extras:
            name = item.get("limitName") or key
            for limit in (item.get("primary") or {}, item.get("secondary") or {}):
                if limit:
                    lines.append(
                        f"| {name} | {_window_label(limit)} | {_fmt_percent(limit)} | {_fmt_until(limit.get('resetsAt'))} |"
                    )
    return "\n".join(lines)


def main() -> int:
    try:
        print(render_usage(_codex_rpc_rate_limits()))
        return 0
    except Exception as exc:
        print(f"❌ Codex usage could not be retrieved: {type(exc).__name__}: {exc}")
        print(f"Sign in again: `{_codex_binary()} login`")
        print(f"Dashboard: {DASHBOARD}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
