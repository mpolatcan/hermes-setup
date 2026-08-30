# GitHub Personal SSH Guard

Credential-free Hermes plugin and installer that prevent agents from reaching GitHub through the host user's personal SSH identity.

## Boundaries

- The plugin blocks direct GitHub SSH routes at the Hermes pre-tool boundary.
- `scripts/hermes-agent-ssh-guard.sh` rejects Git-over-SSH before the host SSH client executes.
- HTTPS access through the approved profile-scoped broker remains available.
- This is an accidental credential-use guard, not a sandbox against arbitrary local code.
- The deployed plugin identity remains `github-transport-guard` for config compatibility.

## Tests

```bash
python3 -m unittest discover \
  -s components/security/github-personal-ssh-guard/tests -v
```

## Install

Dry-run only:

```bash
python3 components/security/github-personal-ssh-guard/install_github_personal_ssh_guard.py
```

Applying the installer changes profile plugin/config state and is outside this source-only taxonomy migration.
