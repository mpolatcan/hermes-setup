# Security & Sandboxing

[← All docs](../README.md)

---

## 13. Safety and operational guardrails

Six agents touching the network and filesystem need explicit guardrails.

**Approvals policy.** In each agent's `config.yaml`:

```yaml
approvals:
  mode: smart    # auxiliary LLM judges risk; escalates genuinely dangerous commands
```

Start with `manual` for the first week to see what gets flagged. Switch to `smart` once you trust the patterns. Never use `off` on the always-on agents.

**Iteration budget.** Default is 90 turns per conversation. For `ops` (which should be short and deterministic), lower it:

```yaml
agent:
  max_turns: 30
```

For `research` (which legitimately needs many tool calls for a thorough job), keep the default or raise to 120.

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

### 13.7 Execution sandboxing — the container is your security boundary

Hermes offers four command-execution backends — `local` (no isolation, runs on the host), `docker`, `modal`, and `daytona` (cloud sandboxes). For bare-metal gateway installs the docs push you toward a sandbox backend, because *"for gateway sessions on the local backend, neither the approval system nor container isolation protects the host."* **We need none of those backends, because our architecture already supplies the sandbox.**

Hermes runs *inside* a per-agent container (OrbStack), so when an agent uses `terminal` or `code_execution` on the default `local` backend, the command executes **inside that container — the macOS host is never touched.** As the docs put it, *"the container itself is the security boundary,"* which is exactly why dangerous-command approval is skipped for container backends. This is the sandboxing payoff of the Section 1 decision: the same choice that gives you isolation gives you the execution sandbox for free.

What that means concretely for the seven agents:

- **No `modal`/`daytona`, no docker-in-docker.** Those exist to isolate Hermes when it runs directly on a host. OrbStack + container-per-agent already do that job. Leave the execution backend at `local` inside each container — adding a nested sandbox would be redundant overhead.
- **Residual exposure = bind mounts only.** A runaway shell can reach only what is mounted into its container: each agent's own `~/.hermes-<name>` data dir, plus `coder`'s `~/projects` and `~/godot-projects`. Mount nothing an agent doesn't need. **Never** mount the Docker socket or a home directory into an agent container — that would hand the agent a path back to the host.
- **The best sandbox is no shell at all.** Per Section 6.6, `terminal` and `code_execution` are disabled on `research`, `concierge`, `writer`, and `producer` — those four have zero command-execution surface. Only `coder` (terminal + Python) and `ops` (terminal) can run commands, and only inside their own containers.
- **Credential stripping is on by default.** Hermes removes environment variables matching `KEY` / `TOKEN` / `SECRET` / `PASSWORD` / `CREDENTIAL` / `AUTH` from the child processes of `terminal` and `code_execution`, so LLM-generated code cannot exfiltrate your API keys through the environment. Do **not** defeat this by adding secrets to `terminal.env_passthrough` or to a skill's env config — both bypass the filter.
- **Keep `code_execution` off everywhere but `coder`.** Beyond the focus argument in 6.6, there is a known disclosure (issue #7071) in which the code-execution sandbox injects the project root into the child's `PYTHONPATH`, which can leak config secrets and security rules into LLM-run code. Disabling it on every agent except `coder` shrinks that exposure to a single isolated container.
- **`--user $(id -u):$(id -g)` on `coder`.** Already specified in Sections 6 and 7. Files the agent writes into mounted directories are owned by you rather than root, and the in-container process runs unprivileged.
- **Approvals are defense-in-depth, not the boundary.** The container is the boundary; the approval mode (`smart`, above) is the application-layer net on top. Run both, not either — if a tailnet device or an agent is ever compromised, the second layer still has to be cleared.

---
