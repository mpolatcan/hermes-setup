# Codex Usage Telegram Command

Canonical, credential-free source for the fleet-wide Telegram `/codex_usage` command.

## What it does

- Registers the menu-visible Hermes plugin command `codex-usage` (Telegram: `/codex_usage`).
- Calls Codex app-server JSON-RPC method `account/rateLimits/read`.
- Produces a Telegram-friendly Markdown usage table.
- Does not scrape the TUI, call the reset-credit consume endpoint, or print account identifiers/tokens.
- Resolves Codex portably via `CODEX_BIN`, `PATH`, `~/.local/bin/codex`, then `/opt/homebrew/bin/codex`.
- Uses the real user `~/.codex` even when a Hermes gateway overrides `HOME` per profile.

## Install or restore across all nine profiles

Dry-run first:

```bash
python3 integrations/codex-usage/install.py
```

Apply after reviewing the plan:

```bash
python3 integrations/codex-usage/install.py --apply
```

The installer:

1. Copies the credential-free plugin into `~/.hermes/plugins/codex-usage`.
2. Creates profile-local symlinks for all nine profiles.
3. Adds `codex-usage` to each block-style `plugins.enabled` list while preserving other entries and comments.
4. Creates backups before replacing a shared plugin or changing config.
5. **Does not restart gateways.** Use Telegram `/restart` per bot after explicit approval when a newly installed plugin must be loaded.

## Authentication

The plugin shares the user's existing Codex ChatGPT OAuth session in `~/.codex`; no credential is stored in this repo. If the command reports `401 token_expired`:

```bash
codex login
```

Complete browser approval on the Mac, then run the test below. No gateway restart is needed for token renewal.

## Verify

```bash
python3 -m unittest discover -s integrations/codex-usage/tests -v
integrations/codex-usage/scripts/codex_usage.py
```

A healthy live probe exits `0` and prints `## Codex Usage — Official` plus the current plan and windows.
