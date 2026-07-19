from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000/v3"
tag = uuid.uuid4().hex[:10]
workspace = f"low-codex-canary-{tag}"
session = f"session-{tag}"
subject = f"subject-{tag}"
observer = f"observer-{tag}"
phrase = f"LANTERN-{tag.upper()}"


def request(method: str, path: str, body: dict | None = None, ok: tuple[int, ...] = (200, 201, 202, 204)):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            if response.status not in ok:
                raise RuntimeError(f"unexpected HTTP {response.status} for {method} {path}")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} for {method} {path}: {detail}") from exc


def delete_canary() -> None:
    try:
        request("DELETE", f"/workspaces/{workspace}/sessions/{session}")
    except Exception:
        pass
    for _ in range(20):
        try:
            request("DELETE", f"/workspaces/{workspace}")
            break
        except Exception as exc:
            if "409" not in str(exc):
                break
            time.sleep(0.5)
    for _ in range(120):
        listing = request("POST", "/workspaces/list", {}) or {}
        items = listing.get("items") or listing.get("workspaces") or listing.get("data") or []
        if not any(isinstance(item, dict) and item.get("id") == workspace for item in items):
            return
        time.sleep(0.5)


container_program = r'''
import asyncio, json, os
from src.config import settings
from src.dialectic.core import DialecticAgent
from src.llm import honcho_llm_call
from src.utils.agent_tools import DIALECTIC_TOOLS

async def main():
    ws=os.environ["CANARY_WS"]
    sess=os.environ["CANARY_SESSION"]
    subject=os.environ["CANARY_SUBJECT"]
    observer=os.environ["CANARY_OBSERVER"]
    phrase=os.environ["CANARY_PHRASE"]

    low=settings.DIALECTIC.LEVELS["low"]
    low.MODEL_CONFIG=settings.DIALECTIC.LEVELS["minimal"].MODEL_CONFIG.model_copy(deep=True)
    settings.DIALECTIC.SESSION_HISTORY_MAX_TOKENS=0

    agent=DialecticAgent(ws,sess,observer,subject,reasoning_level="low")
    query=(
        f"Use the grep_messages tool to find the exact canary phrase {phrase}. "
        "Then answer with only that phrase. Do not guess."
    )
    executor, task_name, run_id, started=await agent._prepare_query(query)
    response=await honcho_llm_call(
        model_config=low.MODEL_CONFIG,
        prompt="",
        max_tokens=settings.DIALECTIC.MAX_OUTPUT_TOKENS,
        tools=DIALECTIC_TOOLS,
        tool_choice=low.TOOL_CHOICE,
        tool_executor=executor,
        max_tool_iterations=low.MAX_TOOL_ITERATIONS,
        messages=agent.messages,
        track_name="Dialectic Low Canary",
        max_input_tokens=settings.DIALECTIC.MAX_INPUT_TOKENS,
        trace_name="dialectic_low_canary",
        telemetry=agent._telemetry_context(),
    )
    if phrase not in response.content:
        raise RuntimeError("non-stream response omitted canary phrase")
    tool_names=[call.get("tool_name") for call in response.tool_calls_made]
    if "grep_messages" not in tool_names:
        raise RuntimeError(f"expected grep_messages tool call, got {tool_names}")

    stream_agent=DialecticAgent(ws,sess,observer,subject,reasoning_level="low")
    chunks=[]
    async for chunk in stream_agent.answer_stream(
        f"Use grep_messages to find {phrase}, then answer with only that phrase. Do not guess."
    ):
        chunks.append(chunk)
    streamed="".join(chunks)
    if phrase not in streamed:
        raise RuntimeError("stream response omitted canary phrase")

    print(json.dumps({
        "status":"pass",
        "catalog_tools":len(DIALECTIC_TOOLS),
        "nonstream_iterations":response.iterations,
        "nonstream_tool_calls":len(response.tool_calls_made),
        "nonstream_tool_names":tool_names,
        "nonstream_input_tokens":response.input_tokens,
        "nonstream_output_tokens":response.output_tokens,
        "stream_chunks":len(chunks),
        "stream_chars":len(streamed),
    }))

asyncio.run(main())
'''

try:
    request("POST", "/workspaces", {"id": workspace})
    request("POST", f"/workspaces/{workspace}/peers", {"id": subject})
    request("POST", f"/workspaces/{workspace}/peers", {"id": observer})
    request(
        "POST",
        f"/workspaces/{workspace}/sessions",
        {
            "id": session,
            "peers": {
                subject: {"observe_me": False, "observe_others": False},
                observer: {"observe_me": False, "observe_others": False},
            },
            "configuration": {
                "reasoning": {"enabled": True},
                "summary": {"enabled": False},
                "dream": {"enabled": False},
                "peer_card": {"create": False, "use": False},
            },
        },
    )
    request(
        "POST",
        f"/workspaces/{workspace}/sessions/{session}/messages",
        {"messages": [{"peer_id": subject, "content": f"The exact canary phrase is {phrase}."}]},
    )
    env_args = [
        "-e", f"CANARY_WS={workspace}",
        "-e", f"CANARY_SESSION={session}",
        "-e", f"CANARY_SUBJECT={subject}",
        "-e", f"CANARY_OBSERVER={observer}",
        "-e", f"CANARY_PHRASE={phrase}",
    ]
    completed = subprocess.run(
        ["docker", "exec", "-i", *env_args, "server-api-1", "/app/.venv/bin/python", "-"],
        input=container_program,
        text=True,
        capture_output=True,
        timeout=240,
    )
    if completed.returncode != 0:
        raise RuntimeError("container canary failed: " + completed.stderr[-3000:])
    print(completed.stdout.strip())
finally:
    delete_canary()
    listing = request("POST", "/workspaces/list", {}) or {}
    items = listing.get("items") or listing.get("workspaces") or listing.get("data") or []
    ids = [item.get("id") for item in items if isinstance(item, dict)]
    cleanup_absent = workspace not in ids
    print(json.dumps({"cleanup_absent": cleanup_absent}))
    if not cleanup_absent:
        raise RuntimeError("canary workspace cleanup did not converge")
