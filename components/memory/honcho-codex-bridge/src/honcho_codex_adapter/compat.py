from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import platform
from dataclasses import dataclass
from typing import Any


CONTRACT_SYMBOLS = {
    "hermes_cli.auth": ("resolve_codex_runtime_credentials",),
    "agent.codex_responses_adapter": (
        "_chat_messages_to_responses_input",
        "_preflight_codex_api_kwargs",
        "_responses_tools",
    ),
    "agent.transports.codex": ("ResponsesApiTransport", "_content_cache_key"),
    "agent.auxiliary_client": ("_codex_cloudflare_headers",),
    "agent.codex_runtime": ("_consume_codex_event_stream",),
}


@dataclass(frozen=True, slots=True)
class HermesContract:
    resolve_credentials: Any
    chat_messages_to_responses_input: Any
    preflight_codex_api_kwargs: Any
    responses_tools: Any
    responses_transport: Any
    content_cache_key: Any
    cloudflare_headers: Any
    consume_event_stream: Any


def load_hermes_contract() -> HermesContract:
    loaded: dict[tuple[str, str], Any] = {}
    for module_name, names in CONTRACT_SYMBOLS.items():
        module = importlib.import_module(module_name)
        for name in names:
            loaded[(module_name, name)] = getattr(module, name)
    return HermesContract(
        resolve_credentials=loaded[("hermes_cli.auth", "resolve_codex_runtime_credentials")],
        chat_messages_to_responses_input=loaded[("agent.codex_responses_adapter", "_chat_messages_to_responses_input")],
        preflight_codex_api_kwargs=loaded[("agent.codex_responses_adapter", "_preflight_codex_api_kwargs")],
        responses_tools=loaded[("agent.codex_responses_adapter", "_responses_tools")],
        responses_transport=loaded[("agent.transports.codex", "ResponsesApiTransport")],
        content_cache_key=loaded[("agent.transports.codex", "_content_cache_key")],
        cloudflare_headers=loaded[("agent.auxiliary_client", "_codex_cloudflare_headers")],
        consume_event_stream=loaded[("agent.codex_runtime", "_consume_codex_event_stream")],
    )


def contract_report() -> dict[str, Any]:
    symbols: dict[str, str] = {}
    for module_name, names in CONTRACT_SYMBOLS.items():
        module = importlib.import_module(module_name)
        for name in names:
            value = getattr(module, name)
            try:
                signature = str(inspect.signature(value))
            except (TypeError, ValueError):
                signature = "<unavailable>"
            symbols[f"{module_name}.{name}"] = signature

    def package_version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    return {
        "schema_version": 1,
        "runtime": {
            "python": platform.python_version(),
            "hermes_agent": package_version("hermes-agent"),
            "openai": package_version("openai"),
        },
        "symbols": symbols,
    }
