# Linear–Hermes Native Platform Adapter

A native Linear Agent Session platform plugin for Hermes Gateway. Linear is the human-facing task and discussion surface; Hermes remains the conversation and execution layer. The same tracked adapter code runs as nine isolated profile-local instances. No separate bridge daemon or Hermes built-in webhook route is used.

## Architecture

```mermaid
flowchart LR
    L["Linear Agent Session"]:::net -->|"HTTPS webhook + HMAC"| C["Cloudflare Named Tunnel<br/>hermes-linear"]:::net
    C -->|"nine exact host/path routes"| A["Profile-local adapters<br/>127.0.0.1:8787–8793,8796–8797"]:::svc
    A -->|"MessageEvent"| G["Nine Hermes Gateways"]:::codex
    G -->|"AgentActivity + lifecycle GraphQL"| L

    classDef net fill:#1976D2,stroke:#0D47A1,color:#fff
    classDef svc fill:#00838F,stroke:#006064,color:#fff
    classDef codex fill:#FFCC80,stroke:#EF6C00,color:#E65100
```

- Plugin registration: Hermes `ctx.register_platform()` API.
- Public transport: Cloudflare Named Tunnel `hermes-linear`, managed by `ai.hermes.cloudflared`.
- Listeners: nine dedicated loopback ports; see the fleet table below.
- Endpoint: `POST /linear/webhook`.
- Health check: `GET /health`.
- The retired Tailscale Funnel sidecar is not a fallback or rollback target. The normal Tailscale app remains private remote access only.

## Security and delivery guarantees

- `Linear-Signature`: HMAC-SHA256 over the exact raw request body.
- Replay protection: `webhookTimestamp`, ±60 seconds by default.
- Tenant pinning: the webhook organization ID must match the organization ID obtained from the OAuth identity.
- Signature rotation: `LINEAR_WEBHOOK_SECRET` plus optional `LINEAR_WEBHOOK_SECRET_PREVIOUS`.
- The pre-auth invalid-signature rate limiter is separate from the authenticated Hermes platform rate limiter.
- Default request-body limit: 256 KiB.
- SQLite claim/done ledger; stale processing claims can be reclaimed after a crash.
- Semantic event keys:
  - `created`: action + Agent Session ID
  - `prompted`: action + Agent Session ID + Agent Activity ID
  - fallback: raw-body hash
- Linear `webhookId` is subscription metadata and is not used as event identity.
- Linear issue, comment, and prompt content is labeled as untrusted user input, never as trusted instructions.

## Files

| Path | Purpose |
|---|---|
| `adapter.py` | Native platform lifecycle, webhook validation, and prompt/stop routing |
| `linear_client.py` | OAuth refresh and Linear GraphQL Agent Activity writes |
| `ledger.py` | Persistent semantic-dedup ledger |
| `plugin.yaml` | Hermes plugin manifest |
| `scripts/install_linear_oauth.py` | Attended localhost PKCE installer (legacy/interactive only) |
| `scripts/linear_mobile_pkce_once.py` | One-shot mobile PKCE installer using 1Password exact-field resolution |
| `tests/test_native_platform.py` | Security, OAuth, prompt, stop, and dedup tests |
| `tests/test_mobile_pkce.py` | Mobile callback, capability, path, no-clobber, and redaction tests |

## Credential architecture

1Password is canonical for static Linear credentials. Each persona's webhook signing secret lives in its profile-scoped 1Password item and is resolved into the gateway process environment through the unattended SDK bootstrap. Process environment values take precedence over the legacy `credential_env_file`; plaintext secret files are not required for new rollouts. Secrets are never copied through chat, clipboard, the repository, Notion, or Linear.

The OAuth file is a necessary native `0600` exception because refresh-token rotation requires atomic writeback:

```text
/Users/mutlupolatcan/.hermes/profiles/<profile>/credentials/linear-oauth.json
```

Runtime variable names mapped to 1Password:

```dotenv
LINEAR_WEBHOOK_SECRET=<current-secret>
LINEAR_WEBHOOK_SECRET_PREVIOUS=<previous-secret-during-rotation-only>
```

The installer writes the OAuth file atomically with mode `0600`. Never log access or refresh tokens, and never copy them into the repository, chat, clipboard, Notion, or Linear.

## OAuth setup

The attended localhost installer is legacy/interactive only and uses:

```text
http://localhost:3000/oauth/callback
```

New profile rollouts use `linear_mobile_pkce_once.py` so consent can be completed from a phone without clipboard or Remote Desktop. Before running it, register the exact temporary HTTPS callback in the Linear application and route only that hostname/path through Cloudflare to the selected localhost port:

```text
https://<profile-oauth-host>/oauth/callback
```

The helper accepts only an exact `op://.../.../LINEAR_CLIENT_ID` reference; there is no raw `--client-id` or clipboard option. `OP_SERVICE_ACCOUNT_TOKEN` must be injected into the process by the profile-scoped Keychain/service-account bootstrap, never typed into chat, shell arguments, or logs.

```bash
/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap/venv/bin/python \
  integrations/linear-hermes-platform/scripts/linear_mobile_pkce_once.py \
  --client-id-reference 'op://<profile-vault>/<profile-item>/LINEAR_CLIENT_ID' \
  --public-base-url 'https://<profile-oauth-host>/oauth' \
  --expected-organization-id '<organization-id>' \
  --destination '/Users/mutlupolatcan/.hermes/profiles/<profile>/credentials/linear-oauth.json' \
  --bind-port <temporary-local-port>
```

Run that command only after the exact Linear redirect and temporary Cloudflare route have been reviewed and approved. The listener binds to `127.0.0.1`, prints a JSON-encoded one-shot `START_URL`, and shuts down after completion, denial, or timeout. Its timeout defaults to and cannot be set below `3600` seconds; the bounded maximum is `86400` seconds. The start URL contains a random capability. Its initial `GET` serves an inert confirmation form without creating PKCE state or consuming the capability, so messaging-app preview crawlers cannot burn the flow. Only the exact form `POST` consumes the capability and returns the Linear authorization redirect; every replay fails closed. The unguessable path capability is the CSRF defense for this one-shot form, and responses use `Referrer-Policy: no-referrer` to prevent path disclosure. Do not log or publish the URL. The helper enforces PKCE S256, exact host/path gates, organization verification, profile-root and symlink confinement, atomic no-clobber installation, and mode `0600`. Remove the temporary public route after the flow. OAuth tokens, the PKCE verifier, and service-account credentials must never enter the repository, chat, clipboard, Notion, or Linear.

## Hermes configuration

Platform section in `~/.hermes/profiles/<profile>/config.yaml` (example: Derya/general; use the profile-specific port and paths for every instance):

```yaml
gateway:
  platforms:
    linear:
      enabled: true
      extra:
        host: 127.0.0.1
        port: 8787
        webhook_path: /linear/webhook
        oauth_file: /Users/mutlupolatcan/.hermes/profiles/general/credentials/linear-oauth.json
        database_path: /Users/mutlupolatcan/.hermes/profiles/general/state/linear-bridge.sqlite3
        max_body_bytes: 262144
        replay_window_seconds: 60
        processing_timeout_seconds: 300
        dedup_retention_seconds: 604800
        preauth_rate_limit_per_minute: 120
        outbox_poll_seconds: 1
        outbox_base_delay_seconds: 2
        outbox_max_delay_seconds: 300
        data_change_events_enabled: true      # live since 0.5.0; selected data events are context/control only
        dependency_wait_enabled: true         # live; blocked sessions resume exactly once after blocker closure
        dependency_poll_seconds: 60           # recovery only; no LLM polling
        issue_status_writeback_enabled: true
        issue_status_mapping:
          queued: Todo
          running: In Progress
          blocked: Blocked
          done: Done
      home_channel:
        platform: linear
        chat_id: <dedicated-agent-session-id>
        name: Linear operational inbox
      gateway_restart_notification: false
```

The plugin source is deployed to each profile-local runtime directory:

```text
/Users/mutlupolatcan/.hermes/profiles/<profile>/plugins/linear/
```

The tracked deployment allowlist is `__init__.py`, `adapter.py`, `ledger.py`, `linear_client.py`, and `plugin.yaml`. Copy only those files from `integrations/linear-hermes-platform/`; never copy tests, caches, credentials, OAuth stores, or SQLite state. The current audit proves `45/45` allowlisted logic files match the tracked source and are mode `0600`; it does **not** prove exact-directory parity. Existing runtime directories also contain `__pycache__`, seven contain `tests/`, and `general/plugins/linear` is mode `0755` while the other eight are `0700`. Those are explicit follow-up drift, not silently cleaned up by this documentation change.

Deployment is an approval-gated operation, not a blind fleet copy. There is intentionally no partial shell recipe here: source review, promotion, rollback and runtime restart must remain one fail-closed procedure. For one named profile:

1. **Pin the reviewed source.** Record the approved commit, require a clean worktree, export the five allowlisted files from that commit (not mutable working-tree paths), and verify the reviewed SHA-256 manifest.
2. **Confine and serialize.** Validate every source, profile, plugin and backup ancestor as a real non-symlink directory under the expected roots; acquire a profile-specific exclusive lock before creating any stage or rollback path.
3. **Stage completely.** Create a unique same-filesystem stage directory at mode `0700`; install exactly the five allowlisted files at `0600`; reject symlinks, missing files, extra entries or hash mismatch.
4. **Preserve rollback.** Create a unique non-existing rollback slot at mode `0700`, print its immutable coordinates before mutation, and preserve the complete previous target there.
5. **Promote atomically.** Rename the complete staged directory into `/Users/mutlupolatcan/.hermes/profiles/<profile>/plugins/linear`. A state-aware `EXIT`/`HUP`/`INT`/`TERM` handler must restore the checked rollback whenever promotion does not reach verified state, while preserving a failed candidate for audit.
6. **Read back.** Verify target path confinement, exact five-file set, directory/file modes and source-manifest hashes after promotion. Release the lock only after this succeeds.
7. **Restart and accept.** Mutlu sends `/restart` in only that profile's Telegram chat; then run its local `/health`, local/public `405/401/404`, signed lifecycle and ledger checks. Keep rollback until acceptance is complete.
8. **Roll back symmetrically.** Stop or gate the named gateway, acquire the same profile lock, validate the exact printed rollback coordinates, preserve the failed current target, atomically restore the prior directory, read back its manifest/modes, release the lock, send `/restart`, and rerun acceptance. Never select “latest backup” heuristically.

A future one-command deploy helper must implement and test every invariant above before replacing this explicit runbook. Creating that helper or cleaning runtime extras is separate code/runtime work and requires its own exact diff and approval.

The read-only fleet audit must report four dimensions separately: allowlisted source/runtime hashes, exact entry sets, symlink status, and directory/file modes. The current accepted evidence is `45/45` logic hashes with all allowlisted files at `0600`; exact entry and directory-mode drift remains open as stated above.

| Persona | Profile | Loopback | Public hostname |
|---|---|---:|---|
| Derya | `general` | `127.0.0.1:8787` | `derya-linear.mutlupolatcan.com` |
| Doruk | `researcher` | `127.0.0.1:8788` | `doruk-linear.mutlupolatcan.com` |
| Tuna | `assistant` | `127.0.0.1:8789` | `tuna-linear.mutlupolatcan.com` |
| Naz | `coder` | `127.0.0.1:8790` | `naz-linear.mutlupolatcan.com` |
| Ozan | `writer` | `127.0.0.1:8791` | `ozan-linear.mutlupolatcan.com` |
| Sarp | `producer` | `127.0.0.1:8792` | `sarp-linear.mutlupolatcan.com` |
| Nilay | `marketing` | `127.0.0.1:8793` | `nilay-linear.mutlupolatcan.com` |
| Defne | `health` | `127.0.0.1:8796` | `defne-linear.mutlupolatcan.com` |
| Murat | `finance` | `127.0.0.1:8797` | `murat-linear.mutlupolatcan.com` |

`data_change_events_enabled` and `dependency_wait_enabled` are live for standard profiles. Defne and Murat keep both flags `false` to preserve the sensitive-profile execution boundary; status writeback remains enabled for their explicit Agent Session lifecycle.

## Localization boundary

Persona names are resolved from each installed Linear app actor at runtime and are never hard-coded in adapter logic. Protocol-facing activity and error text stays in English for consistent vendor/runtime behavior. Turkish copy is limited to the explicitly approved mobile OAuth confirmation UI and is covered by UI tests; credentials, capabilities, IDs, and machine-readable completion markers are not localized.

A gateway restart is required after configuration or plugin changes. Restart only the changed profile. For Derya/general, the default safe operation is Mutlu issuing `/restart` from Telegram.

The normal acknowledgment uses the installed Linear app actor name (`Derya`, `Doruk`, etc.); persona text is never hard-coded. To suppress Hermes' one-time “No home channel is set” notice, configure a dedicated long-lived operational-inbox Agent Session as `gateway.platforms.linear.home_channel.chat_id`. Do not use a disposable task session or an issue ID.

## Persistent outbox and issue status writeback

All outbound `agentActivityCreate` and `issueUpdate` operations are inserted into the same SQLite database before network delivery. The outbox row contains a stable ID, Agent Session aggregate key, per-session sequence, operation, JSON payload, state (`pending`, `in_flight`, `delivered`, or `dead`), attempt count, next-attempt time, and delivery/error timestamps.

Delivery is ordered per Agent Session. A later `Done` write cannot overtake the response activity that proves completion. Stale `in_flight` rows are reclaimed after restart. Retryable failures use exponential backoff capped by `outbox_max_delay_seconds`; Linear `retry_after` wins when supplied. Non-retryable failures become dead letters and appear in `/health` as `status: degraded` without turning the liveness endpoint into a restart loop.

Activity creates use the client-generated `AgentActivityCreateInput.id`, so replay after an ambiguous timeout reuses the same Linear entity ID. Issue state assignment is naturally idempotent. Producer retries use stable outbox IDs for thought, error, and status operations; normal responses receive one persisted UUID when accepted.

Execution-to-issue mapping:

| Execution state | Default Linear state | Trigger |
|---|---|---|
| `queued` | `Todo` | Accepted `created` Agent Session event |
| `running` | `In Progress` | Thought acknowledgement persisted |
| `blocked` | `Blocked` | An unresolved Linear `blocks` relation is durably recorded |
| `done` | `Done` | Successful response durably accepted |

`ProcessingOutcome.FAILURE` and `ProcessingOutcome.CANCELLED` intentionally preserve the current issue state: a transport, model, or cancellation signal does not prove a dependency blocker or completion. Terminal human states (`completed` or `canceled`) and custom human workflow states are never overwritten. Bridge-owned transitions use `Todo(10) -> Blocked(15) -> In Progress(20) -> Done(40)`, allowing automatic `Blocked -> In Progress` resume. State names are resolved against the issue team at delivery time; IDs are not hard-coded.

The code default for `issue_status_writeback_enabled` remains fail-closed `false`; production explicitly enables it after the completed OPS-21 approval and fleet rollout. The outbox code can still be deployed and tested with writeback disabled in a canary configuration.


## Data-change events and dependency waiting

`AgentSessionEvent` remains the only direct execution trigger. When `data_change_events_enabled` is true, signed `Issue`, `IssueRelation`, `Comment`, `IssueLabel`, `Project`, `ProjectUpdate`, `AppUserNotification`, `PermissionChange`, and OAuth revoke events pass organization validation and semantic dedup, but ordinary comments and project updates are context-only and do not start an LLM run. Inbox notification timestamps may use the documented ISO `createdAt` field. Explicit agent mentions must produce Linear's native Agent Session event; this is a live canary requirement, not a comment parser assumption. Events authored by the current Linear app actor are ignored to prevent self-trigger loops. An `issueUnassignedFromYou` notification cancels a durable wait, while OAuth revocation degrades `/health`.

Agent output remains an immutable Agent Activity. Linear renders `response` and `elicitation` activities into the issue comment thread for human visibility; later execution context is reconstructed from frozen Agent Activities rather than editable comments.

When `dependency_wait_enabled` is true, a newly delegated issue is queried for incomplete inverse `blocks` relations before Hermes execution starts. If blockers exist, the adapter:

1. Writes the original prompt, Agent Session, issue, and blocker snapshot to `waiting_executions` in SQLite.
2. Persists an `elicitation` activity naming the blockers; Linear derives `awaitingInput`.
3. Does not enqueue the Hermes run.
4. Reconciles the issue after commit to close the blocker-completed-before-wait race.
5. Reconciles again on signed Issue/IssueRelation updates, with a low-frequency GraphQL recovery poll for missed webhooks.
6. Atomically claims `waiting -> resuming`, reuses the original stable delivery key, and starts the same Agent Session exactly once when no blockers remain.

On resume, the adapter prepends its verified current dependency state before Linear's frozen creation `promptContext`. The snapshot may still contain historical `blocked-by` content; the trusted resume directive prevents that stale state from sending the execution back into wait.

Stop and delegate-removal events cancel a pending wait. Interrupted `resuming` rows return to `waiting` on adapter restart. `/health` reports waiting counts, oldest wait age, and the latest wait error; failed waits degrade health without causing a restart loop. The additive SQLite schema is versioned with `PRAGMA user_version=2`. Back up `linear-bridge.sqlite3*` before first migration and retain the backup until live acceptance completes.

## Public ingress

Cloudflare routes only the exact webhook path; unmatched paths return `404`. The active public endpoints follow the fleet table above. Representative checks are:

```text
https://derya-linear.mutlupolatcan.com/linear/webhook
https://doruk-linear.mutlupolatcan.com/linear/webhook
```

Health and security checks:

```bash
personas=(derya doruk tuna naz ozan sarp nilay defne murat)
ports=(8787 8788 8789 8790 8791 8792 8793 8796 8797)
for index in {0..8}; do
  persona=${personas[$index]}
  port=${ports[$index]}
  local_base="http://127.0.0.1:$port"
  public_base="https://$persona-linear.mutlupolatcan.com"
  curl -fsS "$local_base/health" >/dev/null
  test "$(curl -sS -o /dev/null -w '%{http_code}' "$local_base/linear/webhook")" = 405
  test "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$local_base/linear/webhook")" = 401
  test "$(curl -sS -o /dev/null -w '%{http_code}' "$local_base/not-found")" = 404
  test "$(curl -sS -o /dev/null -w '%{http_code}' "$public_base/linear/webhook")" = 405
  test "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$public_base/linear/webhook")" = 401
  test "$(curl -sS -o /dev/null -w '%{http_code}' "$public_base/not-found")" = 404
done
```

Unsigned webhook requests must return `401`; webhook `GET` must return `405`; hostname root paths must return `404`. Cloudflare Access is not placed in front of Linear webhooks because vendor delivery cannot complete an interactive Access challenge.

## Tests

Use the Hermes-bundled Python; the system Python may not include gateway modules:

```bash
cd /Users/mutlupolatcan/.hermes/source/hermes-setup
/Users/mutlupolatcan/.hermes/runtime/hermes-agent/venv/bin/python \
  -m unittest discover \
  -s integrations/linear-hermes-platform/tests -v
```

Expected result: `71/71 OK` (`38` mobile PKCE + `33` native platform). `/health` exposes the active `data_event_types` allowlist so a rollout can verify the accepted event contract without inspecting source files.

Coverage includes invalid signatures, replay attempts, organization mismatch, semantic dedup, legacy-ledger compatibility, OAuth token refresh and rotation, typed `agentActivity.content.body`, delegation, follow-up prompts, Stop hard-cancel, persistent outbox restart recovery, ordered retries, client-generated activity IDs, response-before-Done ordering, durable waiting recovery, resume-once claims, blocker filtering, context-only data events, self-event suppression, delegate-removal cancellation, dead-letter re-drive, schema versioning, and human-owned status preservation.

## Live acceptance criteria

1. A delegation `created` webhook returns `accepted`.
2. Thought and Hermes response activities appear in Linear.
3. A follow-up prompt reaches the delegated persona/profile and the response returns to Linear.
4. A Stop signal interrupts the active Hermes task through `/stop`.
5. The session becomes `complete`, with no extra error activity or residual process.
6. Retrying the same semantic event does not create duplicate execution.
7. A blocked delegation returns `awaiting_input`, writes one `elicitation`, and starts no Hermes run.
8. Completing the final blocker resumes the same session once; replaying the Issue webhook creates no second run.
9. Selected comments, projects, project updates, issue/project labels, issue attachments, and comment reactions are observed without an LLM run; self-authored events are ignored.
10. Delegate removal and Stop cancel durable waits; restart recovers an interrupted resume.
11. A live cross-agent mention canary proves that Linear emits the target agent's native Agent Session before cross-agent automation is enabled.

## Rollback

1. Set `dependency_wait_enabled: false`, `data_change_events_enabled: false`, and `issue_status_writeback_enabled: false`; queued evidence remains in SQLite.
2. Disable the added Issue/Comment/Label/Project/ProjectUpdate data-change categories in the Linear application, retaining Agent Session events if the base canary remains healthy.
3. If one adapter instance must roll back, disable only that persona's Linear application webhook and Cloudflare hostname route; do not stop the shared connector while another persona remains live.
4. Set `gateway.platforms.linear.enabled: false`.
5. Mutlu issues `/restart` from Telegram.
6. Restore the pre-migration `linear-bridge.sqlite3*` backup only while the gateway is stopped. Do not delete the migrated database until pending/dead outbox rows and waiting executions have been audited.

Rollback never touches the normal Tailscale app or Remote Desktop process. The retired Funnel sidecar, former bridge daemon, and built-in webhook route on `127.0.0.1:8644` remain disabled unless a separate architectural decision explicitly restores them.
