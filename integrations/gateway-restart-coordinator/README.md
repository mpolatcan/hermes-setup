# Gateway Restart Coordinator

External, launchd-managed serialization plane for Hermes gateway restart requests. It is intentionally outside every gateway process tree.

## Contract

- Requesters: only `general` (Derya) and `coder` (Naz). The facade requires both a matching `HERMES_HOME` and a live launchd gateway PID in the caller's process ancestry; no requester argument exists and direct external-shell calls fail closed.
- Targets: the nine known fleet profiles.
- Queue: owner-only SQLite/WAL with full synchronous durability, transition ledger, dependency gates, duplicate coalescing and queued-request supersede.
- Preflight: immutable artifact and rollback SHA-256, expected live PID, `hermes -p <profile> config check`, loopback-only health URL.
- Execution: one global service lock, one serial `launchctl kickstart -k`, bounded new-PID wait, managed-process proof, expected serving version and semantic canary.
- Recovery: a request committed as `restarting` is never blindly restarted. A changed PID resumes verification; an unchanged PID becomes `operator_required`.
- Rollback: this service verifies the immutable rollback coordinate but does not mutate artifacts. Failed acceptance stops at `operator_required`; a reviewed deployment helper may perform rollback separately.

## Request schema

```json
{
  "task_id": "OPS-195-canary-a",
  "target_profile": "assistant",
  "artifact_path": "/absolute/immutable/artifact",
  "artifact_sha256": "64 lowercase hex characters",
  "expected_version": "0.8.20",
  "expected_pid": 12345,
  "rollback_path": "/absolute/immutable/rollback-artifact",
  "rollback_sha256": "64 lowercase hex characters",
  "health_url": "http://127.0.0.1:8796/health",
  "semantic_canary": {"path": "status", "equals": "ok"},
  "dependency_task_id": null,
  "barrier": "activate-before-continue"
}
```

For a same-profile request that depends on a prior restart, set `expected_pid` to `"dependency_new_pid"`; the coordinator resolves it from the succeeded dependency's durable `new_pid` evidence. Missing dependencies are rejected at admission, so dependency cycles cannot be introduced.

## Test

```bash
PYTHONPATH=integrations/gateway-restart-coordinator \
  python3 -W error::ResourceWarning -m unittest \
  integrations/gateway-restart-coordinator/tests/test_restart_coordinator.py -v
```

## Stage and activate

```bash
bash integrations/gateway-restart-coordinator/scripts/install.sh
launchctl bootstrap gui/$(id -u) \
  /Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.gateway-restart-coordinator.plist
launchctl print gui/$(id -u)/ai.hermes.gateway-restart-coordinator
```

If the service is already loaded, use `launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway-restart-coordinator`; never boot out an active Hermes gateway.

Request from an authorized profile:

```bash
/Users/mutlupolatcan/.hermes/services/gateway-restart-coordinator/restartctl.py request /absolute/request.json
```

## Verification

1. `launchctl print` reports a coordinator PID whose parent is launchd.
2. `restartctl.py status` reports `integrity=ok` and no dead outbox rows.
3. Deny canaries from the other seven profile homes return `requester_not_allowed` and add no queue row.
4. For each real request, read the ledger and prove old/new PID, expected health version, semantic status and one terminal outbox event.
5. On failed health, verify one restart only and `operator_required`.

## Rollback

Stop only the coordinator service, preserve its ledger, and return to native/manual emergency restart:

```bash
launchctl bootout gui/$(id -u)/ai.hermes.gateway-restart-coordinator
```

Removing service files or the queue is a separate destructive operation and is not part of normal rollback.
