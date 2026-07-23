# Honcho Codex Adapter Architecture

## Scope

The adapter is a loopback-only OpenAI Chat Completions façade for Honcho's nine
text-generation surfaces. It does not proxy embeddings, expose Hermes tools, start a
Hermes agent, or mutate Hermes configuration. OpenRouter remains the embedding route.

## Runtime and control planes

```mermaid
flowchart LR
    H["Honcho<br/>9 text surfaces"]:::infra
    A["OpenAI-compatible façade<br/>stable contract"]:::svc
    C["Typed adapter config<br/>TOML + env overrides"]:::config
    B["Hermes compatibility backend<br/>private imports isolated"]:::compat
    R["Selected Hermes runtime<br/>version + signature manifest"]:::hermes
    O["Codex OAuth / Responses API"]:::ext
    V["1Password + Keychain"]:::secret
    E["OpenRouter embeddings"]:::ext

    H -->|"Chat Completions"| A
    H -->|"embeddings only"| E
    C --> A
    A --> B
    B --> R
    R --> O
    V -. "runtime-only bearer" .-> A
    V -. "OAuth resolution" .-> R

    classDef infra fill:#7B1FA2,stroke:#4A148C,color:#fff
    classDef svc fill:#00838F,stroke:#006064,color:#fff
    classDef config fill:#455A64,stroke:#263238,color:#fff
    classDef compat fill:#D32F2F,stroke:#B71C1C,color:#fff
    classDef hermes fill:#EF6C00,stroke:#E65100,color:#fff
    classDef ext fill:#00796B,stroke:#004D40,color:#fff
    classDef secret fill:#455A64,stroke:#263238,color:#fff
```

## Dependency rule

Only `compat.py` may import Hermes modules. Only `backends/hermes.py` may construct the
Hermes-backed OpenAI transport. Request translation, scheduler policy, authentication,
output limits, model catalog, and HTTP contracts must not import Hermes internals.

The manifest at `compatibility/hermes-current.json` records the exact eight imported
symbols, their signatures, Python version, Hermes package version, and OpenAI SDK
version. A mismatch fails the candidate upgrade gate before a listener can be promoted.
The manifest is a compatibility declaration, not permission to overwrite production.

## Configuration flow

```mermaid
flowchart TD
    T["config/adapter.toml<br/>tracked non-secret defaults"]:::config
    F["--config absolute path"]:::config
    E["environment overrides<br/>machine-specific only"]:::config
    V["schema + type + invariant validation"]:::gate
    X["effective config<br/>secret-free JSON"]:::svc
    A["FastAPI + driver + scheduler"]:::svc
    S["1Password / Keychain<br/>secrets at runtime"]:::secret

    T --> V
    F --> V
    E --> V
    V --> X
    V --> A
    S -. "never rendered" .-> A

    classDef config fill:#455A64,stroke:#263238,color:#fff
    classDef gate fill:#D32F2F,stroke:#B71C1C,color:#fff
    classDef svc fill:#00838F,stroke:#006064,color:#fff
    classDef secret fill:#455A64,stroke:#263238,color:#fff
```

Precedence is built-in safe defaults, TOML file, then typed environment overrides. The
bearer key is never a TOML field and never appears in `--print-effective-config`.
Unknown top-level keys, non-loopback listeners, invalid model classes, incomplete queue
weights, and malformed values fail closed.

## Replaceable backend

The HTTP façade and `HermesCodexDriver` depend on the narrow `HermesBackend.invoke`
contract. If a Hermes release breaks the private API and adapting it is not justified,
replace that backend with the official Codex App Server backend. Do not weaken loopback,
authentication, schema, tool-call, cancellation, or output-limit controls to preserve a
private import.
