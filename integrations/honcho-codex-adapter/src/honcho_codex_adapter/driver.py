from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .models import ChatCompletionRequest

UPSTREAM_MODEL = "gpt-5.6-luna"
MODEL_ALIASES = {
    "gpt-5.6-luna": UPSTREAM_MODEL,
    "honcho-deriver-luna": UPSTREAM_MODEL,
    "honcho-summary-luna": UPSTREAM_MODEL,
    "honcho-dialectic-minimal-luna": UPSTREAM_MODEL,
    "honcho-dialectic-luna": UPSTREAM_MODEL,
    "honcho-dream-deduction-luna": UPSTREAM_MODEL,
    "honcho-dream-induction-luna": UPSTREAM_MODEL,
}


class AdapterError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502, code: str = "upstream_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(slots=True)
class DriverResult:
    id: str
    created: int
    model: str
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    finish_reason: str
    usage: dict[str, int]

    def as_chat_completion(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": self.content,
                    "tool_calls": self.tool_calls,
                },
                "finish_reason": self.finish_reason,
            }],
            "usage": self.usage,
        }


def _map_response_format(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    if not response_format:
        return None
    kind = response_format.get("type")
    if kind == "text":
        return None
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        spec = response_format.get("json_schema")
        if not isinstance(spec, dict):
            raise AdapterError("response_format.json_schema must be an object", status_code=400, code="invalid_request_error")
        schema = spec.get("schema")
        name = spec.get("name")
        if not isinstance(schema, dict) or not isinstance(name, str) or not name.strip():
            raise AdapterError("json_schema requires non-empty name and object schema", status_code=400, code="invalid_request_error")
        return {
            "type": "json_schema",
            "name": name.strip(),
            "schema": schema,
            "strict": bool(spec.get("strict", True)),
        }
    raise AdapterError(f"unsupported response_format type: {kind!r}", status_code=400, code="unsupported_parameter")


def _map_tool_choice(tool_choice: str | dict[str, Any] | None) -> str | dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice not in {"auto", "none", "required"}:
            raise AdapterError("unsupported tool_choice", status_code=400, code="unsupported_parameter")
        return tool_choice
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        raise AdapterError("unsupported tool_choice object", status_code=400, code="unsupported_parameter")
    fn = tool_choice.get("function")
    name = fn.get("name") if isinstance(fn, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise AdapterError("specific tool_choice requires function.name", status_code=400, code="invalid_request_error")
    return {"type": "function", "name": name.strip()}


def _tool_specs(tools: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for index, tool in enumerate(tools or []):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise AdapterError(f"tools[{index}] must be a function tool", status_code=400, code="invalid_request_error")
        function = tool.get("function")
        if not isinstance(function, dict):
            raise AdapterError(f"tools[{index}].function must be an object", status_code=400, code="invalid_request_error")
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not name.strip():
            raise AdapterError(f"tools[{index}].function.name must be non-empty", status_code=400, code="invalid_request_error")
        name = name.strip()
        if name in specs:
            raise AdapterError(f"duplicate tool name: {name}", status_code=400, code="invalid_request_error")
        if not isinstance(parameters, dict):
            raise AdapterError(f"tool {name} parameters must be an object schema", status_code=400, code="invalid_request_error")
        try:
            Draft202012Validator.check_schema(parameters)
        except SchemaError as exc:
            raise AdapterError(f"tool {name} has an invalid JSON Schema", status_code=400, code="invalid_request_error") from exc
        specs[name] = parameters
    return specs


def _validate_tool_contract(
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    specs = _tool_specs(tools)
    mapped_choice = _map_tool_choice(tool_choice)
    if isinstance(mapped_choice, dict):
        chosen_name = str(mapped_choice["name"])
        if chosen_name not in specs:
            raise AdapterError(f"tool_choice references unknown tool: {chosen_name}", status_code=400, code="invalid_request_error")
    if mapped_choice == "required" and not specs:
        raise AdapterError("tool_choice='required' requires at least one tool", status_code=400, code="invalid_request_error")
    return specs


def validate_upstream_tool_calls(
    tool_calls: list[Any] | None,
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if not tool_calls:
        return None
    specs = _tool_specs(tools)
    validated: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        name = getattr(tool_call, "name", None)
        call_id = getattr(tool_call, "id", None) or f"call_{uuid.uuid4().hex}"
        arguments = getattr(tool_call, "arguments", None)
        if not isinstance(name, str) or name not in specs:
            raise AdapterError("Codex returned an unknown tool call", status_code=502, code="invalid_upstream_tool_call")
        if not isinstance(arguments, str):
            raise AdapterError(f"Codex returned non-JSON arguments for tool {name}", status_code=502, code="invalid_upstream_tool_call")
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"Codex returned malformed JSON arguments for tool {name}", status_code=502, code="invalid_upstream_tool_call") from exc
        if not isinstance(parsed_arguments, dict):
            raise AdapterError(f"Codex returned non-object arguments for tool {name}", status_code=502, code="invalid_upstream_tool_call")
        try:
            Draft202012Validator(specs[name]).validate(parsed_arguments)
        except ValidationError as exc:
            raise AdapterError(f"Codex returned schema-invalid arguments for tool {name}", status_code=502, code="invalid_upstream_tool_call") from exc
        validated.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return validated


def build_upstream_kwargs(request: ChatCompletionRequest, request_id: str) -> dict[str, Any]:
    try:
        from agent.codex_responses_adapter import (
            _chat_messages_to_responses_input,
            _preflight_codex_api_kwargs,
            _responses_tools,
        )
        from agent.transports.codex import _content_cache_key
    except Exception as exc:  # pragma: no cover - environment failure
        raise AdapterError("Hermes Codex transport is unavailable", status_code=503, code="transport_unavailable") from exc

    upstream_model = MODEL_ALIASES.get(request.model)
    if upstream_model is None:
        raise AdapterError("model is not allowed", status_code=404, code="model_not_found")

    messages = [m.model_dump(exclude_none=True) for m in request.messages]
    instruction_parts: list[str] = []
    replay_messages: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") in {"system", "developer"}:
            content = message.get("content")
            if content:
                instruction_parts.append(content if isinstance(content, str) else str(content))
        else:
            replay_messages.append(message)
    instructions = "\n\n".join(instruction_parts).strip() or "You are a helpful assistant."
    _validate_tool_contract(request.tools, request.tool_choice)
    converted_tools = _responses_tools(request.tools)
    effort = request.reasoning_effort or "medium"
    if effort == "minimal":
        effort = "low"

    kwargs: dict[str, Any] = {
        "model": upstream_model,
        "instructions": instructions,
        "input": _chat_messages_to_responses_input(
            replay_messages,
            replay_encrypted_reasoning=True,
            current_issuer_kind="codex_backend",
        ),
        "store": False,
        "reasoning": {"effort": effort, "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "timeout": 120.0,
        "extra_headers": {"session_id": request_id, "x-client-request-id": request_id},
    }
    if converted_tools:
        kwargs["tools"] = converted_tools
        kwargs["tool_choice"] = _map_tool_choice(request.tool_choice) or "auto"
        kwargs["parallel_tool_calls"] = True if request.parallel_tool_calls is None else request.parallel_tool_calls
    cache_key = _content_cache_key(instructions, converted_tools)
    if cache_key:
        kwargs["prompt_cache_key"] = cache_key

    # Validate using Hermes's fail-closed Codex contract before adding the
    # Responses `text` field, which Hermes 2026.7.7.2 preflight does not yet list.
    kwargs = _preflight_codex_api_kwargs(kwargs, allow_stream=False)
    text_config: dict[str, Any] = {}
    fmt = _map_response_format(request.response_format)
    if fmt:
        text_config["format"] = fmt
    if request.verbosity:
        text_config["verbosity"] = request.verbosity
    if text_config:
        kwargs["text"] = text_config
    return kwargs


def _usage_dict(final: Any) -> dict[str, int]:
    usage = getattr(final, "usage", None)
    def val(name: str) -> int:
        raw = getattr(usage, name, 0) if usage is not None else 0
        if isinstance(usage, dict):
            raw = usage.get(name, raw)
        return int(raw or 0)
    prompt = val("input_tokens")
    completion = val("output_tokens")
    total = val("total_tokens") or prompt + completion
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


class HermesCodexDriver:
    """One-request-at-a-time driver over Hermes-managed Codex OAuth."""

    def __init__(self, *, allow_unbounded_output: bool | None = None) -> None:
        if allow_unbounded_output is None:
            raw = os.getenv("HONCHO_CODEX_ALLOW_UNBOUNDED_OUTPUT", "")
            allow_unbounded_output = raw.strip().lower() in {"1", "true", "yes"}
        self.allow_unbounded_output = allow_unbounded_output

    def complete(self, request: ChatCompletionRequest) -> DriverResult:
        requested_cap = request.max_completion_tokens or request.max_tokens
        if requested_cap is not None and not self.allow_unbounded_output:
            raise AdapterError(
                "Codex OAuth backend cannot enforce output token limits; "
                "operator opt-in is required",
                status_code=400,
                code="unsupported_parameter",
            )
        request_id = f"honcho-{uuid.uuid4()}"
        kwargs = build_upstream_kwargs(request, request_id)
        client = None
        event_stream = None
        try:
            from openai import OpenAI
            from hermes_cli.auth import resolve_codex_runtime_credentials
            from agent.auxiliary_client import _codex_cloudflare_headers
            from agent.codex_runtime import _consume_codex_event_stream
            from agent.transports.codex import ResponsesApiTransport

            creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
            token = str(creds.get("api_key") or "").strip()
            base_url = str(creds.get("base_url") or "").strip().rstrip("/")
            if not token or not base_url:
                raise AdapterError("Codex credentials are unavailable", status_code=401, code="authentication_error")

            client = OpenAI(
                api_key=token,
                base_url=base_url,
                default_headers=_codex_cloudflare_headers(token),
                timeout=120.0,
                max_retries=1,
            )
            event_stream = client.responses.create(**kwargs, stream=True)
            final = _consume_codex_event_stream(event_stream, model=UPSTREAM_MODEL)
            if final is None:
                raise AdapterError("Codex stream ended without a final response")
            normalized = ResponsesApiTransport().normalize_response(final, issuer_kind="codex")
            tool_calls = validate_upstream_tool_calls(
                normalized.tool_calls,
                request.tools,
            )
            return DriverResult(
                id=f"chatcmpl-{uuid.uuid4().hex}",
                created=int(time.time()),
                model=request.model,
                content=normalized.content,
                tool_calls=tool_calls,
                finish_reason="tool_calls" if tool_calls else normalized.finish_reason,
                usage=_usage_dict(final),
            )
        except AdapterError:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status == 401 or getattr(exc, "relogin_required", False):
                raise AdapterError("Codex authentication failed", status_code=401, code="authentication_error") from exc
            if status == 429:
                raise AdapterError("Codex quota or rate limit reached", status_code=429, code="rate_limit_error") from exc
            if isinstance(exc, TimeoutError) or exc.__class__.__name__ in {
                "APITimeoutError",
                "ConnectTimeout",
                "ReadTimeout",
                "TimeoutException",
            }:
                raise AdapterError(
                    "Codex upstream request timed out",
                    status_code=504,
                    code="timeout_error",
                ) from exc
            if isinstance(status, int) and 400 <= status < 500:
                raise AdapterError("Codex rejected the request", status_code=status, code="upstream_request_error") from exc
            raise AdapterError("Codex upstream request failed") from exc
        finally:
            if event_stream is not None:
                close = getattr(event_stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
