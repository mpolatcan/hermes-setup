# Component taxonomy

Canonical repository components live at:

```text
components/<domain>/<vendor-or-product>-<capability>/
```

A component name must reveal both what product/vendor it belongs to and what capability it provides. Domain directories group ownership and purpose; they are not catch-alls. New top-level `integrations/`, `misc/`, `utils/`, `helpers/`, or `other/` component buckets are forbidden.

## Canonical catalog

| Domain | Component | Purpose |
|---|---|---|
| Platforms | [`platforms/linear-agent-platform`](platforms/linear-agent-platform/README.md) | Linear Agent Sessions, lifecycle, outbound policy, and plugin deployment |
| Secrets | [`secrets/1password-hermes-bootstrap`](secrets/1password-hermes-bootstrap/README.md) | 1Password SDK-backed Hermes credential bootstrap |
| Memory | [`memory/honcho-codex-bridge`](memory/honcho-codex-bridge/README.md) | Honcho inference to Hermes Codex OAuth bridge |
| Operations | [`operations/gateway-restart-coordinator`](operations/gateway-restart-coordinator/README.md) | Ordered and recoverable gateway restart coordination |
| Security | [`security/github-personal-ssh-guard`](security/github-personal-ssh-guard/README.md) | Guard against agent use of the host's personal GitHub SSH identity |
| Commands | [`commands/codex-usage-command`](commands/codex-usage-command/README.md) | Fleet-wide Telegram `/codex_usage` command |

## Naming contract

1. Add components only under an existing, specific domain or introduce a reviewed domain whose name describes a stable responsibility.
2. Use lowercase kebab-case for domain and component directory names.
3. Component directory names must include the vendor/product and the real capability; avoid opaque names such as `adapter`, `plugin`, `service`, or `tool` by themselves.
4. Executable source tools and installers must also carry vendor/product plus purpose where a generic filename would be ambiguous outside its directory.
5. Keep source paths and deployed runtime paths separate. A source taxonomy rename does not authorize a runtime path, credential, config, LaunchAgent, or service mutation.
6. Update source, tests, installers, deploy helpers, documentation, commands, and relative links in one change. Historical snapshots may retain old paths only when clearly labeled as non-current evidence.

`tests/test_component_taxonomy.py` enforces the canonical set and rejects catch-all or known opaque source names deterministically.
