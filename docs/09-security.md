# Security & Sandboxing

[← All docs](../README.md)

---

## 13. Safety and operational guardrails

Nine agents touching the network and filesystem need explicit guardrails. Managed Hermes v0.19.0 (Quicksilver) resolves both `terminal` and `execute_code` on **all nine profiles** by design (accepted 2026-07-19); approvals are `off` fleet-wide. The live compensating controls are persona guardrails + credential stripping, Tirith on `general` and `researcher`, and the website blocklist on `coder` and `finance` — not a shell-restriction policy.

**Approvals policy.** In each agent's `config.yaml`:

```yaml
approvals:
  mode: 'off'    # current fleet-wide setting — no approval gate on any profile
```

**Current posture: all nine profiles run `approvals.mode: 'off'`**; Tirith pre-exec scanning is enabled on `general` and `researcher`, while the website blocklist is enabled on `coder` and `finance`. With all profiles host-code-capable by design, persona text, credential redaction, Tirith and blocklists reduce risk but do not create a host sandbox; there is no per-command human gate. A graduated `manual`/`smart` policy remains a possible future alternative, not the current state:

```yaml
approvals:
  mode: smart    # auxiliary LLM judges risk; escalates genuinely dangerous commands
```

**Iteration budget.** Default is 90 turns per conversation. For a chat-only agent that should answer in a couple of steps (e.g. `producer` scoring, or a quick `assistant` lookup), lower it:

```yaml
agent:
  max_turns: 30
```

For `researcher` and `marketing` (which legitimately need many tool calls for a thorough research job), keep the default or raise to 120.

**Website blocklist.** If any agent should never touch internal hosts, list them:

```yaml
security:
  website_blocklist:
    enabled: true
    domains:
      - "*.internal.local"
      - "192.168.*"
      - "10.*"
      - "100.*"           # blocks reaching other tailnet devices via web tools
      - "169.254.*"       # link-local; blocks cloud metadata endpoints
```

The `100.*` entry deserves attention specifically because of Tailscale. Without it, an agent's `web_fetch` could hit other tailnet members (your phone, work laptop, other Macs). The live explicit blocklist is currently enabled only on `coder` and `finance`; extending it to other profiles is a future config change that requires review and approval.

**Tirith security scanning** (optional). If you want pre-execution scanning of shell commands:

```yaml
security:
  tirith_enabled: true
  tirith_fail_open: true   # commands run if scanner unavailable; set false for strict mode
```

Requires the `tirith` binary in the host PATH. The live fleet enables it on `general` and `researcher`.

**Secrets redaction.**

```yaml
security:
  redact_secrets: true   # strip API key patterns from logs and tool output
```

**Credential source of truth.** 1Password is canonical for static provider, Telegram, webhook and integration credentials. Hermes configs hold only ID-based `op://` references and resolve them at startup. Do not store real service credentials in profile `.env`, `config.yaml`, chat, clipboard, Notion, Linear or the repository. The 1Password bootstrap identity and writable OAuth stores are the only local `0600` exceptions; full architecture and rotation procedure: [Section 15](15-credential-management.md).

### 13.7 Execution sandboxing — native, there is no container boundary

**This is the security cost of the native, single-install decision (Section 1) — pay attention to it.** Hermes offers four command-execution backends — `local` (no isolation, runs on the host), `docker`, `modal`, and `daytona` (sandboxes). Native on the `local` backend, the docs' warning applies in full: *"for gateway sessions on the local backend, neither the approval system nor container isolation protects the host."* There is no container wrapping the agent — a `terminal` or `code_execution` command runs **directly on your Mac, as your user.**

Because all profiles share one install, a shell-capable agent can read sibling config references, bootstrap identity and writable OAuth stores. Static service credentials are canonical in scoped 1Password vaults, but that does not protect a secret already resolved into a process. All nine profiles are host-code-capable by design, so the residual risk is fleet-wide.

**Threat model — be precise about what you're defending against.** This is single-tenant. The real threat is **prompt-injection-driven exfiltration** — a poisoned web page or repository tricking a shell-capable profile into using a resolved credential, reading local bootstrap/OAuth state, or running a destructive command. 1Password removes stale copies; it is not a runtime sandbox.

The fence around `coder`:

|- **All nine profiles are shell-capable; there is no scope advantage left in targeting only coder.** The v0.19.0 resolver assembles `terminal` and `execute_code` for 9/9 profiles. This is accepted by design — the fence is credential stripping + Tirith + persona guardrails + website blocklist, not a "fewer shells" policy.
|- **Approvals are currently `off`.** There is no enforced per-command human gate. All nine profiles share the host-level residual risk.
|- **Credential stripping is on by default.** Hermes removes env vars matching `KEY` / `TOKEN` / `SECRET` / `PASSWORD` / `CREDENTIAL` / `AUTH` from child processes. Do **not** defeat this via `terminal.env_passthrough` or a skill's env config. 1Password prevents static credential sprawl on disk; stripping limits resolved-value propagation into child processes.
|- **Website blocklist blocks the obvious exfil destinations.** Keep the Section 13 blocklist (`100.*`, `169.254.*`, internal ranges) on active shell profiles so a hijacked agent can't reach a tailnet device or a metadata endpoint to phone home.
|- **Optional: route `coder`'s `code_execution` through the `docker` backend.** If you want the on-disk-secret protection back without losing GPU game-dev, run the *agent* native (Godot GUI on the host) but set its `code_execution` backend to `docker`, so untrusted generated code runs jailed while interactive engine work stays native. This re-introduces Docker for one code path only — worth it if `coder` will execute much third-party code.
|- **OS-user isolation isn't available here.** A dedicated macOS user for `coder` would mean a separate Hermes install. The current fence is tool scope + redaction + blocklist (+ optional docker backend), **not** approvals or a second user account.
|- **`code_execution` `PYTHONPATH` disclosure (issue #7071).** The code-execution path injects the project root into the child's `PYTHONPATH`, which can leak config/security files into LLM-run code. All nine profiles are affected; this is accepted risk mitigated by credential stripping and persona guardrails.
|- **Be honest about what these are: risk reducers, not a sandbox.** In the container draft a kernel boundary contained `coder`; native, nothing does. Approvals, redaction, and the blocklist *lower* the odds and cost of a bad action — they don't make one impossible. Specifically, **the website blocklist does not stop a shell.** It gates browser-style fetches, but any agent with `terminal` + `code_execution` can use `curl`, a Python socket, or any shell command **bypassing the blocklist entirely.** The blocklist helps against the `web`/`browser` path, not the shell path.
|- **For untrusted or third-party code, the `docker` code-exec backend (or a separate machine) is the recommended default.** It limits access to profile state and resolved runtime credentials. Treat `local`-backend code-exec as acceptable only for code authored under your eye.

#### 13.7a — Derya as fleet admin (the highest-privilege exposure)

`general`/Derya was deliberately given `terminal` + `code_execution` + `file` (2026-06-08) so she can configure and tune the fleet — edit configs, restart gateways, batch-change profiles. This is the **single largest concentration of risk in the design**, because unlike `coder` (on-demand, you-driven) Derya is **always-on** and reads the **web and images** — the classic prompt-injection→shell path, on the agent with the most power.

What gates her, and what does *not*:

- **Tirith** catches some dangerous command patterns on `general` (and is also enabled on `researcher`); it is not an approval gate.
- **What is NOT gated:** plain `hermes config set …`, `launchctl kickstart …`, and `file`/`patch` writes can run with no platform prompt. “Show the exact fleet change and wait for yes” is behavioral, not enforced by Hermes.
- **What limits the blast radius:** `code_execution`'s child env is scrubbed of secrets (provider/Telegram keys aren't in the script's environment), the single allowed Telegram user is you, and SSRF protection blocks loopback/RFC1918 fetches.

Honest framing: Derya-as-admin is **convenience traded for surface**. Treat it as "Derya *can* change the fleet and is asked to confirm first," not "Derya *cannot* act without my approval." The hard-guarantee alternative is **proposal-only** (Derya drafts the change, you apply it) — no shell, true gating. If her blast radius ever feels too large, revert her toolsets to read-only and go proposal-only.

### 13.8 Incident response — stop, contain, rotate

Pieces of this live scattered across the plan (token re-issue in [docs/03](03-telegram-bots.md), `invalid_grant` in [docs/04 §5.3](04-models.md)); this is the consolidated drill, written down *before* it's needed so you're not composing `launchctl` invocations while an agent misbehaves.

**Stop the whole fleet now:**

```bash
for p in ~/Library/LaunchAgents/ai.hermes.gateway-*.plist; do
  launchctl bootout gui/$(id -u) "$p"
done
```

Stops and disables every gateway (including the watchdog — fine, you're at the keyboard). Resume per profile with `launchctl bootstrap gui/$(id -u) <plist>`.

**One agent misbehaving (in practice: `coder`):**

1. `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.gateway-coder.plist` — stop just that profile.
2. Read what it did: the session transcript under `~/.hermes/profiles/coder/sessions/` — what ran, and what prompted it.
3. Check what actually executed: recent file changes (`find ~ -newer /tmp/marker`), shell history, and — if it's still live — open connections (`lsof -i`).
4. Tighten before restart: `approvals: manual`, prune toolsets further, extend the blocklist (Section 13).
5. Restart only once you understand the trigger. A restart without a diagnosis re-runs the experiment.

**Credential compromise (or reasonable suspicion):** revoke at the vendor first, rotate the canonical field in 1Password, verify affected `op://` mappings, then restart and smoke-test only the affected profiles. If a shell-capable profile was compromised, include every credential mapped into or resolved by that profile; do not automatically rotate unrelated vaults without evidence.

| Credential | Rotate where | Then |
|---|---|---|
| Telegram bot token | BotFather → `/revoke` | replace that persona's 1Password field; restart only that profile |
| DeepSeek / OpenRouter / other static API key | vendor dashboard | replace the shared or persona 1Password field; verify mappings; restart affected profiles |
| 1Password service-account token | 1Password service accounts | revoke and replace only the profile-local `0600 .op.env` bootstrap token |
| Codex OAuth | ChatGPT password change / sign out all devices | redo native OAuth login; `auth.json` remains the writable `0600` exception |
| MCP / Linear / Notion OAuth | vendor revoke/reauthorize | redo native OAuth flow; token JSON remains the writable `0600` exception; Notion CLI state is canonical under `general/home/.notion/` |

**Why Telegram tokens first:** the bot token *is* the front door — whoever holds it receives your messages and can impersonate the bot to you. `TELEGRAM_ALLOWED_USERS` stops others from commanding your agents, but not from reading what you send. Revoke kills the old token instantly.

**External data planes are separate blast radii.** Honcho stores derived conversation/person memory locally; Linear stores task ownership, execution state, checkpoints, acceptance, and closure; Notion stores durable knowledge, plans, decisions, runbooks, and reports as SaaS state. A Notion OAuth compromise does not expose 1Password values, but it can expose or modify every Notion surface granted to that OAuth identity. Keep `home/.notion/auth.json` at `0600`, never print it, and revoke/reauthorize at Notion on suspicion. See [Notion — Knowledge & Reporting](16-notion-knowledge-and-reporting.md).

---
