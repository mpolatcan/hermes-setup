"""Fail-closed guard against Hermes agents using the host's personal GitHub SSH identity."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_SCANNED_TOOLS = frozenset({"terminal", "execute_code", "code_exec"})
_GITHUB_SSH_PATTERNS = (
    re.compile(r"(?i)(?:^|[\s'\";|&()])(?:git@|ssh://(?:git@)?)(?:ssh\.)?github\.com"),
    re.compile(r"(?i)\b(?:ssh|scp|sftp)\b[^\n\r]*?(?:git@)?(?:ssh\.)?github\.com\b"),
)
_BLOCK_MESSAGE = (
    "GitHub SSH is disabled for Hermes agent sessions because it resolves to the host's "
    "shared personal identity. Use the approved profile-scoped brokered HTTPS route; "
    "profiles without a broker remain no-access."
)


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            yield from _iter_strings(item)


def _contains_github_ssh(value: Any) -> bool:
    return any(
        pattern.search(text)
        for text in _iter_strings(value)
        for pattern in _GITHUB_SSH_PATTERNS
    )


def _pre_tool_call(tool_name: str = "", args: Any = None, **_: Any):
    if tool_name not in _SCANNED_TOOLS or not _contains_github_ssh(args):
        return None
    return {"action": "block", "message": _BLOCK_MESSAGE}


def register(ctx) -> None:
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        raise RuntimeError("github-transport-guard requires pre_tool_call hook support")
    register_hook("pre_tool_call", _pre_tool_call)
