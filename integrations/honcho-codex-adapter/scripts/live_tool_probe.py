from __future__ import annotations

import json

from honcho_codex_adapter.driver import HermesCodexDriver
from honcho_codex_adapter.models import ChatCompletionRequest

TOOL = {
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
        "strict": True,
    },
}

messages = [
    {"role": "system", "content": "Use the supplied tool when required. Never invent tool results."},
    {"role": "user", "content": "Use search_memory with query alpha."},
]
first_request = ChatCompletionRequest.model_validate({
    "model": "honcho-dialectic",
    "messages": messages,
    "tools": [TOOL],
    "tool_choice": {"type": "function", "function": {"name": "search_memory"}},
    "parallel_tool_calls": False,
    "reasoning_effort": "low",
})
first = HermesCodexDriver().complete(first_request)
assert first.finish_reason == "tool_calls", first.finish_reason
assert first.tool_calls and len(first.tool_calls) == 1, first.tool_calls
call = first.tool_calls[0]
assert call["function"]["name"] == "search_memory", call
arguments = json.loads(call["function"]["arguments"])
assert arguments.get("query") == "alpha", arguments

messages.extend([
    {"role": "assistant", "content": first.content, "tool_calls": first.tool_calls},
    {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"matches": ["alpha-result"]})},
])
second_request = ChatCompletionRequest.model_validate({
    "model": "honcho-dialectic",
    "messages": messages,
    "tools": [TOOL],
    "tool_choice": "none",
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "tool_probe_result",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            },
        },
    },
    "reasoning_effort": "low",
})
second = HermesCodexDriver().complete(second_request)
parsed = json.loads(second.content or "")
assert isinstance(parsed.get("result"), str), parsed
assert "alpha-result" in parsed["result"], parsed
print(json.dumps({
    "ok": True,
    "first_finish_reason": first.finish_reason,
    "tool_name": call["function"]["name"],
    "tool_arguments": arguments,
    "second_finish_reason": second.finish_reason,
    "final": parsed,
    "usage": {
        "first": first.usage,
        "second": second.usage,
    },
}, indent=2))
