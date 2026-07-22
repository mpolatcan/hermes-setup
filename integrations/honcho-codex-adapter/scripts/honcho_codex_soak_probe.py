from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ADAPTER_BASE_URL = "http://127.0.0.1:18080"
HONCHO_HEALTH_URL = "http://127.0.0.1:8000/health"
OP_TOKEN_SERVICE = "com.polatcangames.hermes.op-service-account"
OP_TOKEN_ACCOUNT = "general"
OP_API_KEY_URI = "op://xaegpgrxyvqpb7dkrmlxpj2xbe/oe3kgxx7h6fb4axvnnejyw4aoa/jthjqdc63c6svaaiisuz3f3iwm"
EXPECTED_MODELS = {
    "gpt-5.6-luna",
    "honcho-deriver-luna",
    "honcho-summary-luna",
    "honcho-dialectic-minimal-luna",
    "honcho-dialectic-luna",
    "honcho-dream-deduction-luna",
    "honcho-dream-induction-luna",
}
SIMPLE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": "Search memory for a query.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def run_secret_command(command: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"credential command failed: {command[0]}")
    value = completed.stdout.strip()
    if not value:
        raise RuntimeError(f"credential command returned an empty value: {command[0]}")
    return value


def resolve_api_key() -> str:
    configured = os.getenv("HONCHO_CODEX_ADAPTER_API_KEY", "").strip()
    if configured:
        return configured
    token = run_secret_command(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            OP_TOKEN_SERVICE,
            "-a",
            OP_TOKEN_ACCOUNT,
            "-w",
        ]
    )
    env = os.environ.copy()
    env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    return run_secret_command(["/opt/homebrew/bin/op", "read", OP_API_KEY_URI], env=env)


def request_json(
    url: str,
    *,
    api_key: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 240.0,
) -> tuple[dict[str, Any], float]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    method = "GET"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, separators=(",", ":")).encode()
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
    elapsed_ms = (time.perf_counter() - started) * 1000
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("response was not a JSON object")
    return parsed, elapsed_ms


def text_payload(model: str, instruction: str, *, max_tokens: int = 32) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": instruction}],
        "max_completion_tokens": max_tokens,
        "reasoning_effort": "low",
        "stream": False,
    }


def tool_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Call search_memory exactly once with query 'soak canary'. Emit no prose.",
            }
        ],
        "tools": [SIMPLE_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "search_memory"}},
        "parallel_tool_calls": False,
        "reasoning_effort": "low",
        "stream": False,
    }


def structured_payload() -> dict[str, Any]:
    return {
        "model": "honcho-deriver-luna",
        "messages": [
            {
                "role": "user",
                "content": "Return the compact soak-canary object with ok=true and surface='deriver'.",
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "soak_canary",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "surface": {"type": "string", "enum": ["deriver"]},
                    },
                    "required": ["ok", "surface"],
                    "additionalProperties": False,
                },
            },
        },
        "max_completion_tokens": 64,
        "reasoning_effort": "low",
        "stream": False,
    }


def validate_completion(name: str, response: dict[str, Any], expected: str) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("expected exactly one completion choice")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("completion choice was not an object")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("completion message was not an object")
    finish_reason = choice.get("finish_reason")
    if expected == "text":
        if finish_reason != "stop" or not str(message.get("content") or "").strip():
            raise ValueError(f"{name} did not return a completed text response")
    elif expected == "structured":
        if finish_reason != "stop":
            raise ValueError(f"{name} finish_reason was {finish_reason!r}")
        parsed = json.loads(str(message.get("content") or ""))
        if parsed != {"ok": True, "surface": "deriver"}:
            raise ValueError(f"{name} returned an unexpected structured object")
    elif expected == "tool":
        calls = message.get("tool_calls")
        if finish_reason != "tool_calls" or not isinstance(calls, list) or len(calls) != 1:
            raise ValueError(f"{name} did not return exactly one tool call")
        tool_call = calls[0]
        if not isinstance(tool_call, dict):
            raise ValueError(f"{name} returned a malformed tool call")
        function = tool_call.get("function")
        if not isinstance(function, dict) or function.get("name") != "search_memory":
            raise ValueError(f"{name} returned the wrong tool")
        if json.loads(str(function.get("arguments") or "")) != {"query": "soak canary"}:
            raise ValueError(f"{name} returned the wrong tool arguments")
    else:
        raise ValueError(f"unknown expected response kind: {expected}")
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return {
        "finish_reason": finish_reason,
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
    }


def run_probe(timeout: float) -> dict[str, Any]:
    api_key = resolve_api_key()
    checks: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    def check(name: str, operation: Any) -> None:
        try:
            detail, latency_ms = operation()
            checks.append({"name": name, "ok": True, "latency_ms": round(latency_ms, 3), **detail})
        except Exception as exc:
            failures.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})

    def health(url: str) -> tuple[dict[str, Any], float]:
        response, latency = request_json(url, timeout=30)
        if response.get("status") != "ok":
            raise ValueError(f"unexpected health response: {response!r}")
        return {"status": "ok"}, latency

    def models() -> tuple[dict[str, Any], float]:
        response, latency = request_json(f"{ADAPTER_BASE_URL}/v1/models", api_key=api_key, timeout=30)
        data = response.get("data")
        ids = {
            model_id
            for item in data
            if isinstance(item, dict) and isinstance((model_id := item.get("id")), str)
        } if isinstance(data, list) else set()
        if ids != EXPECTED_MODELS:
            raise ValueError(f"model catalog drift: {sorted(ids)!r}")
        return {"model_count": len(ids)}, latency

    def completion(name: str, payload: dict[str, Any], expected: str) -> Any:
        def execute() -> tuple[dict[str, Any], float]:
            response, latency = request_json(
                f"{ADAPTER_BASE_URL}/v1/chat/completions",
                api_key=api_key,
                payload=payload,
                timeout=timeout,
            )
            return validate_completion(name, response, expected), latency

        return execute

    check("adapter_health", lambda: health(f"{ADAPTER_BASE_URL}/healthz"))
    check("honcho_health", lambda: health(HONCHO_HEALTH_URL))
    check("model_catalog", models)
    check(
        "summary",
        completion(
            "summary",
            text_payload("honcho-summary-luna", "Reply with exactly: SOAK_OK"),
            "text",
        ),
    )
    check("deriver", completion("deriver", structured_payload(), "structured"))
    check(
        "dialectic_minimal",
        completion(
            "dialectic_minimal",
            text_payload("honcho-dialectic-minimal-luna", "Reply with exactly: SOAK_OK"),
            "text",
        ),
    )
    check(
        "dialectic",
        completion("dialectic", tool_payload("honcho-dialectic-luna"), "tool"),
    )
    check(
        "dream_deduction",
        completion("dream_deduction", tool_payload("honcho-dream-deduction-luna"), "tool"),
    )
    check(
        "dream_induction",
        completion("dream_induction", tool_payload("honcho-dream-induction-luna"), "tool"),
    )
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ok": not failures,
        "checks": checks,
        "failures": failures,
    }


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        if line.strip():
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                records.append(parsed)
    return records


def append_history(path: Path, record: dict[str, Any]) -> list[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    return load_history(path)


def render_failure(day: int, target_days: int, record: dict[str, Any]) -> str:
    names = ", ".join(failure["name"] for failure in record["failures"])
    return f"⚠️ **Honcho Codex soak {day}/{target_days}**\n\nBaşarısız kontroller: `{names}`"


def render_final(records: list[dict[str, Any]], target_days: int) -> str:
    considered = records[-target_days:]
    checks = [check for record in considered for check in record.get("checks", [])]
    model_checks = [check for check in checks if check["name"] not in {"adapter_health", "honcho_health", "model_catalog"}]
    latencies = [float(check["latency_ms"]) for check in model_checks]
    input_tokens = sum(int(check.get("input_tokens") or 0) for check in model_checks)
    output_tokens = sum(int(check.get("output_tokens") or 0) for check in model_checks)
    successful_days = sum(bool(record.get("ok")) for record in considered)
    return (
        "## Honcho Codex — 7 günlük soak\n\n"
        "| Metrik | Sonuç |\n"
        "|---|---:|\n"
        f"| Başarılı gün | {successful_days}/{target_days} |\n"
        f"| Model probe | {len(model_checks)} |\n"
        f"| Hata | {sum(len(record.get('failures', [])) for record in considered)} |\n"
        f"| Latency p50 | {statistics.median(latencies):.0f} ms |\n"
        f"| Latency p95 | {percentile(latencies, 0.95):.0f} ms |\n"
        f"| Input token | {input_tokens} |\n"
        f"| Output token | {output_tokens} |"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and optionally record the Honcho Codex 7-day soak probe.")
    parser.add_argument("--history", type=Path)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--target-days", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--json", action="store_true", dest="emit_json")
    args = parser.parse_args()
    if args.record and args.history is None:
        parser.error("--record requires --history")
    if args.target_days < 1:
        parser.error("--target-days must be positive")

    record = run_probe(args.timeout)
    records = append_history(args.history, record) if args.record else [record]
    day = min(len(records), args.target_days)
    if args.emit_json:
        print(json.dumps(record, indent=2, sort_keys=True))
    elif args.record and len(records) >= args.target_days:
        parts = []
        if not record["ok"]:
            parts.append(render_failure(day, args.target_days, record))
        parts.append(render_final(records, args.target_days))
        print("\n\n".join(parts))
    elif not record["ok"]:
        print(render_failure(day, args.target_days, record))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
