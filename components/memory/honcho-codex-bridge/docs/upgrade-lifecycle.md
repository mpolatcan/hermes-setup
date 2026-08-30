# Hermes Upgrade Lifecycle for the Honcho Codex Adapter

This lifecycle isolates candidate Hermes releases from the production adapter. It is
additional to the fleet-wide Hermes upgrade runbook; passing it does not authorize a
production launcher edit or restart.

```mermaid
flowchart LR
    U["Candidate Hermes release"]:::hermes
    S["Side-by-side staging<br/>production unchanged"]:::svc
    C["8-symbol import +<br/>signature contract"]:::gate
    T["Adapter tests +<br/>Honcho guard tests"]:::gate
    P["Temporary :18081 listener<br/>authenticated probe"]:::gate
    E["Dream envelope/effect/E2E"]:::gate
    A{"Explicit operator approval"}:::decision
    R["Launcher switch + restart"]:::deploy
    V["Production verification"]:::ok
    B["Previous runtime rollback"]:::rollback

    U --> S --> C --> T --> P --> E --> A
    A -->|approved| R --> V
    A -->|rejected| B
    V -->|failure| B

    classDef hermes fill:#EF6C00,stroke:#E65100,color:#fff
    classDef svc fill:#00838F,stroke:#006064,color:#fff
    classDef gate fill:#7B1FA2,stroke:#4A148C,color:#fff
    classDef decision fill:#FFB300,stroke:#E65100,color:#263238
    classDef deploy fill:#1976D2,stroke:#0D47A1,color:#fff
    classDef ok fill:#388E3C,stroke:#1B5E20,color:#fff
    classDef rollback fill:#D32F2F,stroke:#B71C1C,color:#fff
```

## Candidate gate

1. Fetch official `origin/main`, record its exact commit, and install that same
   commit side by side using a supported Hermes installation path. Do not run
   an in-place update against the live runtime. Immediately before official
   updater promotion, fetch again and stop if `origin/main` moved; a changed
   target must be restaged and retested.
2. Require the candidate checkout to match official `NousResearch/hermes-agent`
   `origin/main` with zero local commits and zero behavioral source diff. Local
   plugin/config/wrapper behavior is verified separately and is never applied to
   the candidate core.
3. Run `scripts/stage_hermes_upgrade.sh /absolute/path/to/candidate-python
   /absolute/path/to/adapter-config /absolute/path/to/adapter-python
   <expected-official-sha>`.
   The stage script runs adapter config, compatibility, test and deterministic
   probe gates directly against the clean candidate. Historical files under
   `patches/hermes-agent/` are not an upgrade input.
4. Review every manifest mismatch. Never regenerate the manifest merely to make the
   gate green; first inspect source and behavior changes.
5. If the mismatch is understood and the adapter has been updated, run the test suite
   again and write the reviewed manifest with `scripts/check_hermes_compat.py
   --write-manifest`.
6. Start a separately approved temporary adapter on `127.0.0.1:18081` with the
   candidate runtime and a vault-resolved bearer.
7. Run the authenticated deterministic probe, all Dream envelope checks, the
   disposable effect canary, full Dream E2E, and the 11 Honcho completion-guard tests.
8. Require zero fixture residue and preserve logs without credentials.
9. Require an independent successful `hermes backup --quick --output <exact-path>`;
   the archive must be non-empty and pass `unzip -t` before updater mutation.
   Show the official updater command, backup/rollback coordinates, any launcher diff,
   and restart command. Runtime promotion, launcher changes and restart are separate
   explicit operator approvals.
10. After the updater returns but before any gateway restart, require promoted core
   `HEAD == expected-official-sha`. A mismatch is never served: restore the previous
   managed release/backup and restage the new upstream target.
11. After promotion, verify one listener, both health endpoints, the four-route
   catalog, all nine Honcho routes, recent logs, and the deterministic probe.

## Rollback boundary

Keep the previous runtime available until production verification and soak complete.
Rollback changes only the selected runtime/config pointer; it does not change Honcho's
HTTP contract. Show the exact restore diff and restart command before acting. Liveness
alone is insufficient: repeat authenticated model, structured output, tool, Dream, and
cleanup checks.

## Installation support note

Current official Hermes documentation lists `install.sh`, Hermes Desktop, and git clone
as supported installation paths, and lists Homebrew/pip package installs as unsupported.
The current Homebrew deployment is therefore a migration concern, not a target pattern.
No script in this integration hardcodes a versioned Cellar directory.

- <https://hermes-agent.nousresearch.com/docs/getting-started/platform-support>
- <https://hermes-agent.nousresearch.com/docs/getting-started/updating>
