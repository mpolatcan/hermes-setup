from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEDUCTION_ROUTE = "honcho-dream"
INDUCTION_ROUTE = "honcho-dream"
EXPECTED_TOOL_NAMES = {
    "deduction": [
        "get_recent_observations",
        "search_memory",
        "search_messages",
        "create_observations_deductive",
        "delete_observations",
        "update_peer_card",
    ],
    "induction": [
        "get_recent_observations",
        "search_memory",
        "search_messages",
        "create_observations_inductive",
    ],
}
EXPECTED_ARGUMENTS: dict[str, dict[str, Any]] = {
    "get_recent_observations": {"limit": 3, "session_only": False},
    "search_memory": {"query": "sandbox canary", "top_k": 2},
    "search_messages": {"query": "sandbox canary", "limit": 2},
    "create_observations_deductive": {
        "observations": [
            {
                "content": "Sandbox deduction",
                "source_ids": ["obs-sandbox-a"],
                "premises": ["Sandbox premise A"],
            }
        ]
    },
    "delete_observations": {"observation_ids": ["obs-sandbox-delete"]},
    "update_peer_card": {"content": ["IDENTITY: Name: Sandbox Peer"]},
    "create_observations_inductive": {
        "observations": [
            {
                "content": "Sandbox pattern",
                "source_ids": ["obs-sandbox-a", "obs-sandbox-b"],
                "sources": ["Sandbox evidence A", "Sandbox evidence B"],
                "pattern_type": "tendency",
                "confidence": "low",
            }
        ]
    },
}


@dataclass(frozen=True)
class ToolCase:
    specialist: str
    model: str
    target: str
    tools: list[dict[str, Any]]
    arguments: dict[str, Any]


def load_catalogs(honcho_root: Path) -> dict[str, list[dict[str, Any]]]:
    python = honcho_root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(f"Honcho Python not found: {python}")
    code = """
import json
from src.dreamer.specialists import DEDUCTION_SPECIALIST_TOOLS, INDUCTION_SPECIALIST_TOOLS
print(json.dumps({"deduction": DEDUCTION_SPECIALIST_TOOLS, "induction": INDUCTION_SPECIALIST_TOOLS}))
"""
    clean_env = os.environ.copy()
    for inherited_name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        clean_env.pop(inherited_name, None)
    completed = subprocess.run(
        [str(python), "-c", code],
        cwd=honcho_root,
        env=clean_env,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"Could not load Honcho tool catalogs: {detail}")
    loaded = json.loads(completed.stdout)
    if not isinstance(loaded, dict):
        raise RuntimeError("Honcho tool catalog export was not an object")
    return loaded


def validate_catalogs(
    catalogs: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    normalized: dict[str, list[dict[str, Any]]] = {}
    for specialist, expected_names in EXPECTED_TOOL_NAMES.items():
        raw_tools = catalogs.get(specialist)
        if not isinstance(raw_tools, list):
            raise ValueError(f"Missing {specialist} tool catalog")
        names = [tool.get("name") for tool in raw_tools if isinstance(tool, dict)]
        if names != expected_names:
            raise ValueError(
                f"{specialist} tool catalog drift: expected {expected_names}, got {names}"
            )
        converted: list[dict[str, Any]] = []
        for tool in raw_tools:
            schema = tool.get("input_schema")
            if not isinstance(schema, dict):
                raise ValueError(f"Tool {tool.get('name')} has no object input_schema")
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": str(tool.get("description") or ""),
                        "parameters": schema,
                    },
                }
            )
        normalized[specialist] = converted
    return normalized


def build_cases(catalogs: dict[str, list[dict[str, Any]]]) -> list[ToolCase]:
    cases: list[ToolCase] = []
    for specialist, model in (
        ("deduction", DEDUCTION_ROUTE),
        ("induction", INDUCTION_ROUTE),
    ):
        for target in EXPECTED_TOOL_NAMES[specialist]:
            cases.append(
                ToolCase(
                    specialist=specialist,
                    model=model,
                    target=target,
                    tools=catalogs[specialist],
                    arguments=EXPECTED_ARGUMENTS[target],
                )
            )
    return cases


def build_payload(case: ToolCase) -> dict[str, Any]:
    expected = json.dumps(case.arguments, separators=(",", ":"), sort_keys=True)
    return {
        "model": case.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "This is a tool-envelope canary. Emit exactly the selected tool "
                    "call with exactly the supplied JSON arguments. Do not execute a "
                    "tool, invent a result, emit prose, or call another tool."
                ),
            },
            {
                "role": "user",
                "content": f"Call {case.target} exactly once with arguments {expected}",
            },
        ],
        "tools": case.tools,
        "tool_choice": {
            "type": "function",
            "function": {"name": case.target},
        },
        "parallel_tool_calls": False,
        "reasoning_effort": "low",
        "stream": False,
    }


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("HONCHO_CODEX_ADAPTER_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError("Adapter response was not a JSON object")
    return parsed


def validate_response(case: ToolCase, response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError(f"Expected one choice, got {choices!r}")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "tool_calls":
        raise ValueError(f"Expected finish_reason=tool_calls, got {choice!r}")
    message = choice.get("message")
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        raise ValueError(f"Expected exactly one tool call, got {tool_calls!r}")
    tool_call = tool_calls[0]
    function = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(function, dict) or function.get("name") != case.target:
        raise ValueError(f"Expected tool {case.target}, got {function!r}")
    raw_arguments = function.get("arguments")
    if not isinstance(raw_arguments, str):
        raise ValueError("Tool arguments were not a JSON string")
    arguments = json.loads(raw_arguments)
    if arguments != case.arguments:
        raise ValueError(f"Argument mismatch: expected {case.arguments!r}, got {arguments!r}")
    return {
        "specialist": case.specialist,
        "model": case.model,
        "tool": case.target,
        "arguments": arguments,
        "finish_reason": choice["finish_reason"],
        "usage": response.get("usage"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate all Honcho Dream tool envelopes without executing mutations."
    )
    parser.add_argument("--honcho-root", type=Path, required=True)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:18080/v1/chat/completions",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    catalogs = validate_catalogs(load_catalogs(args.honcho_root.resolve()))
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for case in build_cases(catalogs):
        try:
            response = post_json(args.url, build_payload(case), args.timeout)
            result = validate_response(case, response)
            results.append(result)
            print(f"PASS {case.specialist}:{case.target}", file=sys.stderr, flush=True)
        except Exception as exc:
            failures.append(
                {
                    "specialist": case.specialist,
                    "tool": case.target,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            print(
                f"FAIL {case.specialist}:{case.target}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    print(
        json.dumps(
            {
                "ok": not failures,
                "mode": "envelope_only_no_tool_execution",
                "url": args.url,
                "passed": len(results),
                "total": len(results) + len(failures),
                "results": results,
                "failures": failures,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
