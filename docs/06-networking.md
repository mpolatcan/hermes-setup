# Networking — Telegram & (optional) Tailscale

[← All docs](../README.md)

---

```mermaid
flowchart LR
    phone["📱 Phone · Telegram app"]:::user --- tg(["Telegram cloud"]):::net
    tg --- mini["🖥️ Mac Mini M4<br/>native gateways · outbound only"]:::mini
    phone -. "optional · dashboard/HTTP only" .-> ts(["Tailnet · WireGuard"]):::net
    ts -. "bound to 100.x" .-> mini
    pub["🌐 Public internet / LAN"]:::danger -. blocked .- mini
    classDef user fill:#303F9F,stroke:#1A237E,color:#fff
    classDef net fill:#1976D2,stroke:#0D47A1,color:#fff
    classDef mini fill:#388E3C,stroke:#1B5E20,color:#fff
    classDef danger fill:#D32F2F,stroke:#B71C1C,color:#fff
```

## 8. Networking

Everything runs on **one machine** (the Mac Mini M4), and the primary interface is **Telegram**. That changes the networking story completely from the old two-machine, container-per-agent draft.

### 8.1 Telegram needs no inbound ports and no Tailscale

Each native gateway connects **outbound** to Telegram via **long polling**. Telegram's own infrastructure relays your messages, so the agents are reachable **from anywhere** — home, mobile data, traveling — **without exposing a single port** on the Mini and **without Tailscale**. This is the default and it covers ~100% of normal use. (Telegram's *webhook* mode would require an inbound HTTPS endpoint — we don't use it; long polling is what keeps the no-inbound-ports guarantee.)

Because agent-to-agent communication is now **local** (same install — see [Section 17](12-agent-comms.md)), there is also **no inbound network path between agents** to secure. The old per-gateway HTTP API + Tailscale mesh existed to let containers on two machines talk; that requirement is gone.

### 8.2 Tailscale is optional — only for the dashboard or raw HTTP API

You only need Tailscale if you turn on a feature that **listens** and you want to reach it from your phone/laptop:

- the **Hermes web dashboard** (a browser fleet console — see 8.2.1)
- the **HTTP API server** for an external (non-Telegram) client

Hermes has no auth on these by default, so any port reachable from the public internet is a complete-access port. If you enable one, **do not expose it publicly** — put it behind Tailscale. Nous Research's own guidance: *"Do not expose application ports publicly; use SSH tunnels, Caddy with HTTPS, or Tailscale."*

If you never enable the dashboard or HTTP API, **skip this whole section** — Telegram is enough.

#### 8.2.1 The web dashboard — build, multi-profile, persistence

The dashboard is **one console for the whole fleet**, not one-per-agent: the UI has a profile **list + switcher** (`/api/profiles`, `/api/profiles/active`) and a **unified sessions view aggregated across all profiles**, plus per-profile config / API-key editing and create/delete. The `-p <slug>` flag only sets which profile is *selected on load*. Config/keys stay *stored* per-profile (that's the isolation); the dashboard is just one window onto all of them. (Cross-*agent* activity is the kanban board's job — separate, and CLI/TUI only.)

⚠️ **Formula gap (re-verified v0.18.2 / 2026.7.7.2):** the Homebrew package still ships neither the built frontend (`hermes_cli/web_dist/`) nor its `web/` source. Build it once from the matching official GitHub tag and point `HERMES_WEB_DIST` at the output (keeps it outside the brew cellar, which `brew upgrade` wipes):

```bash
# clone just web/ at the tag matching the current install (v0.18.2 → v2026.7.7.2)
git clone --depth 1 --branch v2026.7.7.2 --filter=blob:none --sparse \
  https://github.com/NousResearch/hermes-agent.git ~/hermes-dashboard-src
git -C ~/hermes-dashboard-src sparse-checkout set web
cd ~/hermes-dashboard-src/web && npm install && npm run build   # → ../hermes_cli/web_dist
```

Run it persistently under launchd (`~/Library/LaunchAgents/ai.hermes.dashboard.plist`, `RunAtLoad`+`KeepAlive`, bound `127.0.0.1:9119`), with `HERMES_WEB_DIST` and a node-capable `PATH` in `EnvironmentVariables`, and `--skip-build` so it serves the prebuilt dist:

```bash
hermes -p general dashboard --no-open --skip-build --host 127.0.0.1 --port 9119
# env: HERMES_WEB_DIST=/Users/<you>/hermes-dashboard-src/hermes_cli/web_dist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.dashboard.plist
```

On `brew upgrade hermes-agent`: the cellar's `web_dist` is irrelevant (we use `HERMES_WEB_DIST`), but rebuild the source dir against the **new** tag so the frontend matches the backend — `git -C ~/hermes-dashboard-src fetch --tags && git checkout v<new> && (cd web && npm run build)` — then `launchctl kickstart -k gui/$(id -u)/ai.hermes.dashboard`.

### 8.3 If you do enable a listener — bind it to Tailscale

Install Tailscale on the Mini and your phone (`brew install --cask tailscale`), sign in to the same tailnet, give the Mini a stable hostname (`hermes-mini`), enable MagicDNS.

```bash
tailscale status
tailscale ip -4          # the Mini's tailnet IP, 100.x.y.z
```

Native, there is **no Docker `-p` to bind a host IP** — you bind inside Hermes config. For a profile that exposes the API server or dashboard, in its `config.yaml`:

```yaml
api_server:
  enabled: true
  host: "100.x.y.z"          # the Mini's Tailscale IP — NOT 0.0.0.0, NOT the LAN IP
  port: 8642
  key: "<openssl rand -hex 32>"   # always set a key, even behind Tailscale
```

Binding `host` to the tailnet IP makes the listener reachable from your tailnet devices and **invisible to the LAN and the public internet**. Set a `key` regardless — Tailscale is the network boundary, the key is the application boundary; if a tailnet device is compromised the attacker still needs the key. Generate one per profile (`openssl rand -hex 32`).

For HTTPS (needed to PWA-install a dashboard on your phone), bind the **dashboard** to loopback (`host: "127.0.0.1"`, port `9119`) and let `tailscale serve` terminate TLS in front of it:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:9119
```

(Use this loopback-plus-`tailscale serve` pattern for the dashboard; the direct tailnet-IP bind above is for the API server when you don't want a TLS front. Don't bind a service to the tailnet IP *and* point `tailscale serve` at `localhost` — pick one.)

### 8.4 Verifying the bind (only if you exposed something)

After enabling a listener, confirm it is **not** on `0.0.0.0`:

```bash
# Should show the Tailscale IP, NOT 0.0.0.0 or *
sudo lsof -iTCP -sTCP:LISTEN -P -n | grep -E '8642|9119'
```

External smoke test from a device **not** on the tailnet (cellular, Tailscale off):

```bash
curl http://<mini-public-ip>:8642/health   # must fail / time out
```

If that succeeds, you have a public-facing agent — stop and fix the `host` binding before continuing.

### 8.5 Risks worth knowing

- **The default (Telegram only) exposes nothing** — the safest posture. Prefer it; enable listeners only when you actually want the dashboard.
- **Tailscale ACLs are off by default** ("all tailnet members reach all ports"). Fine for a personal tailnet; tighten in the admin console for a shared one (work/family).
- **Tailscale IP can change** if you remove/rejoin the tailnet. MagicDNS hostnames are more stable than raw IPs; re-bind config if it changes.
- **Telegram bot tokens are the real perimeter.** Since Telegram is the front door, a leaked bot token = a path to that agent. Keep each token canonical in its persona-scoped 1Password item, map it only to that profile, and restrict each bot to your user ID (Sections 4, 9 and 15).

---
