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
```

The test suite reads Honcho's current `src/utils/agent_tools.py` through a
restricted AST extractor and checks every tool schema plus each active catalog
subset. Override its location with `HONCHO_AGENT_TOOLS_PATH` when needed.

## Start locally

Provide the local bearer key via the process environment or the studio vault; never place it in this repository.

```bash
export HONCHO_CODEX_ADAPTER_API_KEY='[FROM VAULT]'
PYTHONPATH=src /opt/homebrew/Cellar/hermes-agent/2026.7.7.2/libexec/bin/python \
  -m honcho_codex_adapter.cli
```

`HONCHO_CODEX_QUEUE_CAPACITY` bounds active plus waiting requests (default `8`); overflow fails immediately with HTTP `503` / `queue_full`.

Active adapter base URL: `http://host.docker.internal:18080/v1`. Seven of nine Honcho inference routes resolve through this adapter. Dream deduction and induction remain on OpenRouter pending their dedicated tool/write-effect validation; embeddings also remain on OpenRouter.

## Current limitations

- Streaming is final-only SSE; the upstream request is still streamed and robustly assembled by Hermes.
- Client disconnect does not yet cancel the blocking upstream thread.
- Codex OAuth rejects Responses `max_output_tokens`. Limited requests fail closed by default; an approved pilot may explicitly set `HONCHO_CODEX_ALLOW_UNBOUNDED_OUTPUT=1`, which is unbounded upstream and does not provide hard-cap/quota parity.
- Tool calls are fail-closed: returned names and JSON arguments are validated against the caller's schema before Honcho can execute them.
- Hermes transport imports are private and must be protected by contract tests on every Hermes upgrade.
- If the private contract fails, retain the HTTP façade and replace the driver with the official Codex App Server fallback.

## Official references

- [Codex authentication](https://developers.openai.com/codex/auth)
- [Responses API](https://developers.openai.com/api/reference/responses/overview/)
- [Create a response](https://developers.openai.com/api/reference/resources/responses/methods/create/)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
