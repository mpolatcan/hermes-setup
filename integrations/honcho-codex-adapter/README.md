# Honcho Codex Adapter

Honcho-only OpenAI Chat Completions façade over Hermes-managed `openai-codex` OAuth. It does **not** start Hermes AIAgent, register tools, mutate Hermes config, or proxy embeddings.

## Runtime contract

- Bind: `127.0.0.1:18080`
- Upstream allowlist: `gpt-5.6-luna` only
- Concurrency: `1`
- Endpoints: `/healthz`, `/v1/models`, `/v1/chat/completions`
- Credentials: resolved per request by `hermes_cli.auth.resolve_codex_runtime_credentials()`
- Transport: Hermes `ResponsesApiTransport` plus its event-stream assembler
- Embeddings: remain on OpenRouter

The adapter deliberately runs with the Hermes Homebrew Python so imports are pinned to the installed Hermes release.

## Development

```bash
HERMES_PY=/opt/homebrew/Cellar/hermes-agent/2026.7.7.2/libexec/bin/python
PYTHONPATH=src "$HERMES_PY" -m unittest discover -s tests -v
PYTHONPATH=src "$HERMES_PY" scripts/live_probe.py
PYTHONPATH=src "$HERMES_PY" scripts/honcho_dream_tool_canary.py \
  --honcho-root /absolute/path/to/honcho-stack/server
docker compose -f /absolute/path/to/honcho-stack/server/docker-compose.yml \
  exec -T api python - < scripts/honcho_dream_effect_canary.py
```

The test suite reads Honcho's current `src/utils/agent_tools.py` through a
restricted AST extractor and checks every tool schema plus each active catalog
subset. Override its location with `HONCHO_AGENT_TOOLS_PATH` when needed.
The Dream canary loads the live Honcho deduction/induction catalogs and validates
all ten tool-call envelopes through the adapter. It never supplies tool results or
executes the returned calls, so observation and peer-card mutations are impossible.
The Dream effect canary is the separate mutation gate: it creates a uniquely prefixed
disposable workspace, invokes Honcho's real executor for deductive and inductive
observation writes, peer-card update, and observation delete, verifies a failed write
leaves zero mutations, retries that intent once, replays update/delete for idempotent
state, then cascade-deletes the fixture and requires zero workspace/document/peer
residue.

## Start locally

Provide the local bearer key via the process environment or the studio vault; never place it in this repository.

```bash
export HONCHO_CODEX_ADAPTER_API_KEY='[FROM VAULT]'
PYTHONPATH=src /opt/homebrew/Cellar/hermes-agent/2026.7.7.2/libexec/bin/python \
  -m honcho_codex_adapter.cli
```

`HONCHO_CODEX_QUEUE_CAPACITY` bounds active plus waiting requests (default `8`); overflow fails immediately with HTTP `503` / `queue_full`. The single-worker scheduler uses weighted priority (`Dialectic 8 : Summary 4 : Deriver 2 : Dream 1`), FIFO within each class, and a starvation override after `HONCHO_CODEX_QUEUE_AGING_SECONDS` (default `30`). Upstream calls default to a `90` second timeout with automatic SDK retries disabled so one stalled request cannot occupy the sole worker for multiple timeout windows; `HONCHO_CODEX_UPSTREAM_TIMEOUT_SECONDS` and `HONCHO_CODEX_UPSTREAM_MAX_RETRIES` provide explicit operator overrides.

Active adapter base URL: `http://host.docker.internal:18080/v1`. All nine Honcho text-generation surfaces resolve through this adapter: Deriver, Summary, five Dialectic levels, and both Dream specialists. The seven advertised model IDs collapse those surfaces onto `gpt-5.6-luna`; embeddings remain on OpenRouter and are intentionally outside this adapter.

## Operations

See [MAINTENANCE.md](MAINTENANCE.md) for the production route matrix, health checks, test and canary sequence, LaunchAgent lifecycle, soak monitoring, Hermes-upgrade gate, and rollback procedure.

## Current limitations

- Streaming is final-only SSE; the upstream request is still streamed and robustly assembled by Hermes.
- Client disconnect propagates through Hermes' `interrupt_check`; partial output is rejected and stream/client resources are closed.
- Non-empty `stop` fails closed. Codex OAuth rejects Responses `max_output_tokens`, so the adapter enforces caller limits on visible output with the `o200k_base` tokenizer: plain text is truncated with `finish_reason=length`, while over-limit structured JSON or tool calls fail closed with `output_limit_exceeded` instead of returning corrupted contracts. Upstream usage remains unmodified for honest quota telemetry.
- Tool calls are fail-closed: returned names and JSON arguments are validated against the caller's schema before Honcho can execute them.
- Hermes transport imports are private and must be protected by contract tests on every Hermes upgrade.
- If the private contract fails, retain the HTTP façade and replace the driver with the official Codex App Server fallback.

## Official references

- [Codex authentication](https://developers.openai.com/codex/auth)
- [Responses API](https://developers.openai.com/api/reference/responses/overview/)
- [Create a response](https://developers.openai.com/api/reference/resources/responses/methods/create/)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
