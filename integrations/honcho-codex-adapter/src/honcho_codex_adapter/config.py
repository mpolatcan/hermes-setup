from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

QUEUE_CLASSES = ("dialectic", "summary", "deriver", "dream")


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 18080
    access_log: bool = False


@dataclass(frozen=True, slots=True)
class UpstreamConfig:
    model: str = "gpt-5.6-luna"
    timeout_seconds: float = 90.0
    max_retries: int = 0


@dataclass(frozen=True, slots=True)
class AdmissionConfig:
    capacity: int = 8
    aging_seconds: float = 30.0
    weights: dict[str, int] = field(
        default_factory=lambda: {"dialectic": 8, "summary": 4, "deriver": 2, "dream": 1}
    )


@dataclass(frozen=True, slots=True)
class OutputConfig:
    encoding: str = "o200k_base"


@dataclass(frozen=True, slots=True)
class ModelRoute:
    upstream_model: str
    queue_class: str


_DEFAULT_ROUTES = {
    "honcho-deriver": ModelRoute("gpt-5.6-luna", "deriver"),
    "honcho-summary": ModelRoute("gpt-5.6-luna", "summary"),
    "honcho-dialectic": ModelRoute("gpt-5.6-luna", "dialectic"),
    "honcho-dream": ModelRoute("gpt-5.6-luna", "dream"),
}


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    schema_version: int = 1
    server: ServerConfig = field(default_factory=ServerConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    models: dict[str, ModelRoute] = field(default_factory=lambda: dict(_DEFAULT_ROUTES))

    def effective_dict(self) -> dict[str, Any]:
        return asdict(self)

    def effective_json(self) -> str:
        return json.dumps(self.effective_dict(), indent=2, sort_keys=True)


DEFAULT_CONFIG = AdapterConfig()


def _strict_keys(name: str, value: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown {name} config keys: {', '.join(sorted(unknown))}")


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a table")
    return value


def _env(name: str, fallback: Any, cast):
    value = os.getenv(name)
    if value is None:
        return fallback
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        expected = "a number" if cast is float else "an integer" if cast is int else "a valid value"
        raise ValueError(f"{name} must be {expected}") from exc


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected a boolean")


def _default_config_path() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "config" / "adapter.toml"
    return candidate if candidate.is_file() else None


def load_config(path: str | os.PathLike[str] | None = None) -> AdapterConfig:
    selected = Path(path).expanduser() if path else None
    if selected is None and os.getenv("HONCHO_CODEX_CONFIG"):
        selected = Path(os.environ["HONCHO_CODEX_CONFIG"]).expanduser()
    if selected is None:
        selected = _default_config_path()

    raw: dict[str, Any] = {}
    if selected is not None:
        if not selected.is_file():
            raise ValueError(f"adapter config not found: {selected}")
        with selected.open("rb") as handle:
            raw = tomllib.load(handle)
    _strict_keys("root", raw, {"schema_version", "server", "upstream", "admission", "output", "models"})

    schema_version = int(raw.get("schema_version", 1))
    if schema_version != 1:
        raise ValueError(f"unsupported schema_version: {schema_version}")

    server_raw = _section(raw, "server")
    _strict_keys("server", server_raw, {"host", "port", "access_log"})
    server = ServerConfig(
        host=_env("HONCHO_CODEX_HOST", server_raw.get("host", "127.0.0.1"), str),
        port=_env("HONCHO_CODEX_PORT", server_raw.get("port", 18080), int),
        access_log=_env("HONCHO_CODEX_ACCESS_LOG", server_raw.get("access_log", False), _bool),
    )

    upstream_raw = _section(raw, "upstream")
    _strict_keys("upstream", upstream_raw, {"model", "timeout_seconds", "max_retries"})
    upstream = UpstreamConfig(
        model=_env("HONCHO_CODEX_UPSTREAM_MODEL", upstream_raw.get("model", "gpt-5.6-luna"), str),
        timeout_seconds=_env(
            "HONCHO_CODEX_UPSTREAM_TIMEOUT_SECONDS", upstream_raw.get("timeout_seconds", 90.0), float
        ),
        max_retries=_env("HONCHO_CODEX_UPSTREAM_MAX_RETRIES", upstream_raw.get("max_retries", 0), int),
    )

    admission_raw = _section(raw, "admission")
    _strict_keys("admission", admission_raw, {"capacity", "aging_seconds", "weights"})
    weights_raw = admission_raw.get("weights", DEFAULT_CONFIG.admission.weights)
    if not isinstance(weights_raw, Mapping):
        raise ValueError("admission.weights must be a table")
    _strict_keys("admission.weights", weights_raw, set(QUEUE_CLASSES))
    weights = {name: int(weights_raw.get(name, DEFAULT_CONFIG.admission.weights[name])) for name in QUEUE_CLASSES}
    admission = AdmissionConfig(
        capacity=_env("HONCHO_CODEX_QUEUE_CAPACITY", admission_raw.get("capacity", 8), int),
        aging_seconds=_env("HONCHO_CODEX_QUEUE_AGING_SECONDS", admission_raw.get("aging_seconds", 30.0), float),
        weights=weights,
    )

    output_raw = _section(raw, "output")
    _strict_keys("output", output_raw, {"encoding"})
    output = OutputConfig(
        encoding=_env("HONCHO_CODEX_OUTPUT_ENCODING", output_raw.get("encoding", "o200k_base"), str)
    )

    models_raw = raw.get("models")
    routes: dict[str, ModelRoute]
    if models_raw is None:
        routes = dict(_DEFAULT_ROUTES)
    elif not isinstance(models_raw, Mapping) or not models_raw:
        raise ValueError("models must be a non-empty table")
    else:
        routes = {}
        for route_id, route_raw in models_raw.items():
            if not isinstance(route_raw, Mapping):
                raise ValueError(f"models.{route_id} must be a table")
            _strict_keys(f"models.{route_id}", route_raw, {"upstream_model", "queue_class"})
            routes[str(route_id)] = ModelRoute(
                upstream_model=str(route_raw.get("upstream_model", upstream.model)),
                queue_class=str(route_raw.get("queue_class", "deriver")),
            )

    if not server.host or server.port < 1 or server.port > 65535:
        raise ValueError("server host and port must define a valid listener")
    if server.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("server.host must remain loopback-only")
    if upstream.timeout_seconds <= 0:
        raise ValueError("upstream.timeout_seconds must be greater than 0")
    if upstream.max_retries < 0:
        raise ValueError("upstream.max_retries must be at least 0")
    if admission.capacity < 1 or admission.aging_seconds <= 0:
        raise ValueError("admission capacity and aging_seconds must be positive")
    if set(weights) != set(QUEUE_CLASSES) or any(value < 1 for value in weights.values()):
        raise ValueError("all admission weights must be positive")
    for route_id, route in routes.items():
        if not route_id.strip() or not route.upstream_model.strip():
            raise ValueError("workload route IDs and upstream models must be non-empty")
        if route.queue_class not in QUEUE_CLASSES:
            raise ValueError(f"models.{route_id}.queue_class is invalid: {route.queue_class}")

    return AdapterConfig(
        schema_version=schema_version,
        server=server,
        upstream=upstream,
        admission=admission,
        output=output,
        models=routes,
    )
