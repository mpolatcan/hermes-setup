"""Coordinator-first gateway restart request tool and direct-command guard."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

RESTARTCTL = Path("/Users/mutlupolatcan/.hermes/services/gateway-restart-coordinator/restartctl.py")
AUTHORIZED_HOMES = frozenset({"general", "coder"})
_SCANNED_TOOLS = frozenset({"terminal", "execute_code", "code_exec", "write_file", "patch"})
_BLOCK_MESSAGE = (
    "Direct model-driven gateway restart is disabled for this profile. "
    "Use request_gateway_restart so the external Restart Coordinator can serialize, "
    "verify, coalesce, recover, and roll back safely. Human /restart remains an "
    "emergency fallback when the coordinator is unavailable or operator_required."
)
_HERMES_RESTART = re.compile(r"(?is)\bgateway\s+restart\b")
_LAUNCHCTL_GATEWAY_MUTATION = re.compile(
    r"(?is)\blaunchctl\s+(?:kickstart|bootout|bootstrap|remove|stop|start|kill)\b[^\n\r]*?"
    r"ai\.hermes\.gateway-(?!restart-coordinator\b)[a-z0-9_-]+"
)
_DIRECT_GATEWAY_SIGNAL = re.compile(
    r"(?is)\b(?:kill|pkill)\b[^\n\r]*?(?:SIG)?(?:USR1|TERM)[^\n\r]*?(?:hermes|gateway|[0-9]{2,})"
)

SCHEMA = {
    "name": "request_gateway_restart",
    "description": (
        "Enqueue one validated gateway restart request through the external Restart Coordinator. "
        "This tool never restarts a process directly and derives requester identity from HERMES_HOME "
        "plus live gateway ancestry."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_id": {"type": "string", "minLength": 1},
            "target_profile": {
                "type": "string",
                "enum": ["general", "assistant", "researcher", "coder", "writer", "producer", "marketing", "health", "finance"],
            },
            "artifact_path": {"type": "string", "minLength": 1},
            "artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "expected_version": {"type": "string", "minLength": 1},
            "expected_pid": {"oneOf": [{"type": "integer", "minimum": 1}, {"const": "dependency_new_pid"}]},
            "rollback_path": {"type": "string", "minLength": 1},
            "rollback_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "health_url": {"type": "string", "pattern": "^http://(?:127\\.0\\.0\\.1|localhost)(?::[0-9]+)?/"},
            "semantic_canary": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string", "minLength": 1}, "equals": {}},
                "required": ["path", "equals"],
            },
            "dependency_task_id": {"type": ["string", "null"]},
            "barrier": {"type": ["string", "null"]},
        },
        "required": [
            "task_id", "target_profile", "artifact_path", "artifact_sha256",
            "expected_version", "expected_pid", "rollback_path", "rollback_sha256",
            "health_url", "semantic_canary",
        ],
    },
}


def _profile_from_home() -> str:
    home = Path(os.environ.get("HERMES_HOME", "")).resolve()
    return home.name if home.parent.name == "profiles" else ""


def _requirements_available() -> bool:
    return _profile_from_home() in AUTHORIZED_HOMES and RESTARTCTL.is_file()


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _iter_strings(item)


def _command_text(text: str) -> str:
    """Normalize common argv/code punctuation without executing or parsing shell input."""
    return re.sub(r"[\[\](),'\"]+", " ", text)


def _pre_tool_call(tool_name: str = "", args: Any = None, **_: Any):
    if tool_name not in _SCANNED_TOOLS:
        return None
    for text in _iter_strings(args):
        normalized = _command_text(text)
        patterns = (_HERMES_RESTART, _LAUNCHCTL_GATEWAY_MUTATION, _DIRECT_GATEWAY_SIGNAL)
        if any(pattern.search(normalized) for pattern in patterns):
            return {"action": "block", "message": _BLOCK_MESSAGE}
    return None


def _request_gateway_restart(payload: dict[str, Any]) -> str:
    if not _requirements_available():
        return json.dumps({"status": "rejected", "reason": "restart_coordinator_unavailable"}, sort_keys=True)
    fd, name = tempfile.mkstemp(prefix="hermes-restart-request-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        child_env = {
            "HERMES_HOME": os.environ.get("HERMES_HOME", ""),
            "HOME": os.environ.get("HOME", ""),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/Users/mutlupolatcan/.local/bin",
        }
        completed = subprocess.run(
            [sys.executable, str(RESTARTCTL), "request", name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=child_env,
        )
        raw = (completed.stdout if completed.returncode == 0 else completed.stderr).strip()
        if raw:
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict) and isinstance(decoded.get("status"), str):
                    return json.dumps(decoded, sort_keys=True)
            except json.JSONDecodeError:
                pass
        return json.dumps(
            {"status": "rejected", "reason": "restart_facade_failed", "exit_code": completed.returncode},
            sort_keys=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return json.dumps({"status": "rejected", "reason": type(exc).__name__}, sort_keys=True)
    finally:
        Path(name).unlink(missing_ok=True)


def _handler(args: dict[str, Any], **_: Any) -> str:
    return _request_gateway_restart(dict(args))


def register(ctx) -> None:
    if not callable(getattr(ctx, "register_hook", None)):
        raise RuntimeError("gateway-restart-request requires pre_tool_call hook support")
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_tool(
        name="request_gateway_restart",
        toolset="gateway_restart",
        schema=SCHEMA,
        handler=_handler,
        check_fn=_requirements_available,
        description=SCHEMA["description"],
        emoji="↻",
    )
