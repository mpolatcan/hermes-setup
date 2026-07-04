# Security & Sandboxing

[← All docs](../README.md)

---

## 13. Safety and operational guardrails

Nine agents touching the network and filesystem need explicit guardrails. **Three can run code:** `coder` (terminal + Python — game dev), `general`/Derya (terminal + Python + `file` — the **fleet admin**, and the highest-privilege agent: always-on *and* web-facing *and* a host shell — §13.7a), and `finance` (fenced Python only). The other six have no command-execution surface.

**Approvals policy.** In each agent's `config.yaml`:

```yaml
approvals:
  mode: 'off'    # current fleet-wide setting — no approval gate on any profile
```

**Current posture: all nine profiles run `approvals.mode: 'off'`**, including the three shell-capable agents (`coder`, `general`, `finance`); Tirith pre-exec scanning is enabled on `general` only. This trades the approval gate for zero-friction operation. What that means concretely: on the shell-capable agents, the layers between LLM output and your Mac are credential redaction, the website blocklist, Tirith (on `general`), and the SOUL's behavioral rules — there is no per-command human gate. §13.7's analysis of that trade still applies in full; the graduated alternative if the risk calculus ever changes is `manual` on the shell agents (`coder`/`general`/`finance`) and `smart` elsewhere:

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

The `100.*` entry deserves attention specifically because of Tailscale. Without it, an agent's `web_fetch` could hit other tailnet members (your phone, work laptop, other Macs). The agent isn't supposed to do that, but the network boundary won't stop it — only the blocklist will. Add this entry for every agent unless one has a specific reason to talk to another tailnet device.

**Tirith security scanning** (optional). If you want pre-execution scanning of shell commands:

```yaml
security:
  tirith_enabled: true
  tirith_fail_open: true   # commands run if scanner unavailable; set false for strict mode
```

Requires the `tirith` binary in the container's PATH.

**Secrets redaction.**

```yaml
security:
  redact_secrets: true   # strip API key patterns from logs and tool output
```

### 13.7 Execution sandboxing — native, there is no container boundary

**This is the security cost of the native, single-install decision (Section 1) — pay attention to it.** Hermes offers four command-execution backends — `local` (no isolation, runs on the host), `docker`, `modal`, and `daytona` (sandboxes). Native on the `local` backend, the docs' warning applies in full: *"for gateway sessions on the local backend, neither the approval system nor container isolation protects the host."* There is no container wrapping the agent — a `terminal` or `code_execution` command runs **directly on your Mac, as your user.**

And because all profiles share one install, a shell-capable agent can read **any sibling profile's `~/.hermes/profiles/<profile>/.env`** — every bot token and API key in the fleet. The old container-per-agent draft closed that gap with a kernel boundary; native, the gap is open. We accept it deliberately, for one reason it was worth it (Section 1): `coder` needs the Metal GPU and the Godot GUI, which a macOS container cannot provide. So the residual risk concentrates on the **few agents that run code** — and we fence those instead of the whole fleet. (At first that was just `coder`; `finance` later added fenced Python, and `general`/Derya was given an admin shell — §13.7a. The fence applies to each.)

**Threat model — be precise about what you're defending against.** This is single-tenant: every token `coder` could read is *yours*. One profile reading another's `.env` is you reading your own data, not a confidentiality breach. The real threat is **prompt-injection-driven exfiltration** — a poisoned web page or repository tricking `coder` into sending your keys somewhere, or running a destructive command. The guardrails below target that.

The fence around `coder`:

- **The best sandbox is no shell at all.** Per Section 6.6, `terminal` and `code_execution` are disabled on `researcher`, `assistant`, `marketing`, `writer`, `producer`, and `health` — **six of the nine agents have zero command-execution surface.** Three can run code: `coder` (terminal + Python), `general`/Derya (terminal + Python + `file` — fleet admin, §13.7a), and `finance` (fenced `code_execution` only, no shell). Toolset pruning is the primary control now that there is no container.
- **`approvals: smart` is mandatory on `coder`.** With no container boundary, the approval layer is the *only* gate between LLM-generated code and your Mac. It judges risk and escalates dangerous commands — especially the network sends that an exfiltration attempt needs. Run `manual` for `coder`'s first week.
- **Credential stripping is on by default.** Hermes removes env vars matching `KEY` / `TOKEN` / `SECRET` / `PASSWORD` / `CREDENTIAL` / `AUTH` from the child processes of `terminal` and `code_execution`, so generated code can't read your keys *from the environment*. Do **not** defeat this via `terminal.env_passthrough` or a skill's env config. (Note: this protects the environment, not `.env` files on disk — see the optional docker backend below for that.)
- **Website blocklist blocks the obvious exfil destinations.** Keep the Section 13 blocklist (`100.*`, `169.254.*`, internal ranges) on `coder` so a hijacked agent can't reach a tailnet device or a metadata endpoint to phone home.
- **Optional: route `coder`'s `code_execution` through the `docker` backend.** If you want the on-disk-secret protection back without losing GPU game-dev, run the *agent* native (Godot GUI on the host) but set its `code_execution` backend to `docker`, so untrusted generated code runs jailed while interactive engine work stays native. This re-introduces Docker for one code path only — worth it if `coder` will execute much third-party code.
- **OS-user isolation isn't available here.** A dedicated macOS user for `coder` would mean a *separate* Hermes install (a different `~/.hermes`), which forfeits the single-install benefits — shared Honcho continuity and native `kanban` across the pipeline. So the fence is approvals + redaction + blocklist (+ optional docker backend), **not** a second user account. If `coder`'s blast radius ever feels too large, the clean escalation is to move it back to its own machine/install — not to fragment users on this one.
- **`code_execution` `PYTHONPATH` disclosure (issue #7071).** The code-execution path injects the project root into the child's `PYTHONPATH`, which can leak config/security files into LLM-run code. Keeping `code_execution` on `coder` only confines that to the one agent you already watch.
- **Be honest about what these are: risk reducers, not a sandbox.** In the container draft a kernel boundary contained `coder`; native, nothing does. Approvals, redaction, and the blocklist *lower* the odds and cost of a bad action — they don't make one impossible. Specifically, **the website blocklist does not stop a shell.** It gates browser-style fetches, but `coder` also has `terminal` + `code_execution`, so `curl`, a Python socket, or any shell command **bypasses the blocklist entirely.** The blocklist helps against the `web`/`browser` path, not the shell path.
- **For untrusted or third-party code, the `docker` code-exec backend (or a separate machine) is the recommended default, not an option.** If `coder` will run code it didn't write — installing packages, executing a cloned repo — route `code_execution` through the `docker` backend so that code can't read your `.env` files on disk or `curl` your keys out. Treat `local`-backend code-exec as acceptable only for code `coder` itself authored under your eye. And `approvals: off` on `coder` is equivalent to handing an LLM your shell — never do it.

#### 13.7a — Derya as fleet admin (the highest-privilege exposure)

`general`/Derya was deliberately given `terminal` + `code_execution` + `file` (2026-06-08) so she can configure and tune the fleet — edit configs, restart gateways, batch-change profiles. This is the **single largest concentration of risk in the design**, because unlike `coder` (on-demand, you-driven) Derya is **always-on** and reads the **web and images** — the classic prompt-injection→shell path, on the agent with the most power.

What gates her, and what does *not*:

- **Manual approvals + Tirith** catch *dangerous-pattern* commands (`rm -rf`, `curl|sh`, writes to `/etc`, killing a gateway). Useful against a blatant injection.
- **What is NOT gated:** plain `hermes config set …`, `launchctl kickstart …`, and `file`/`patch` writes to `honcho.json`/`SOUL.md`/sibling configs are *not* dangerous patterns — they **run with no prompt**. So "every config change is approved by me" is **behavioral** (her SOUL is told to show the change and wait for a yes), **not enforced** by Hermes. A clever injection can talk past the SOUL.
- **Self-disable path:** with a shell she can run `hermes config set approvals.mode off` un-prompted. The built-in file-guard blocks editing her *own* `config.yaml` via `file`, but not via the shell. SOUL says never; nothing enforces it.
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

**Credential compromise (or reasonable suspicion):** rotate in blast-radius order. And remember 13.7 — every shell-capable agent can read every profile's `.env`, so **if `coder` or `general` was compromised, assume *all* keys leaked and rotate all of them**, not just its own. (`general`/Derya is always-on and web-facing, so it's the likeliest compromise vector — §13.7a.)

| Credential | Rotate where | Then |
|---|---|---|
| Telegram bot tokens (×9) | BotFather → `/revoke` per bot | rerun `setup-bots.sh` with new tokens (rewrites `~/.hermes/profiles/<slug>/.env`), restart gateways |
| DeepSeek API key | platform.deepseek.com | update all `.env`s (`DEEPSEEK_API_KEY` fans to the seven DeepSeek-primary profiles) |
| MiniMax API key (dormant) | platform.minimax.io | still present in every `.env` though no profile uses `provider: minimax` — rotate or remove |
| OpenRouter key | openrouter.ai → key settings | update aux/fallback config |
| Codex OAuth | ChatGPT password change / "sign out all devices" (revokes the refresh token) | redo login Path A/B per [docs/04 §5.3](04-models.md) — `invalid_grant` in logs is expected until you do |
| TinyFish key | TinyFish dashboard | update the MCP config ([docs/08](08-web-search.md)) |

**Why Telegram tokens first:** the bot token *is* the front door — whoever holds it receives your messages and can impersonate the bot to you. `TELEGRAM_ALLOWED_USERS` stops others from commanding your agents, but not from reading what you send. Revoke kills the old token instantly.

---
