#!/usr/bin/env python3
"""Install the coordinator-first restart plugin only for general and coder."""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
from pathlib import Path

PROFILES = ("general", "coder")
PLUGIN = "gateway-restart-request"


def enable_plugin_text(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    changed = False
    plugin_idx = next((i for i, line in enumerate(lines) if line.rstrip() == "plugins:" and not line.startswith((" ", "\t"))), None)
    entry = f"    - {PLUGIN}\n"
    if plugin_idx is None:
        suffix = "" if text.endswith("\n") or not text else "\n"
        return text + suffix + f"plugins:\n  enabled:\n{entry}", True
    end = next((i for i in range(plugin_idx + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))), len(lines))
    for index in range(plugin_idx + 1, end):
        line = lines[index]
        if line.rstrip("\n") in {"  enabled: []", "  disabled: []"}:
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = line.rstrip("\n").removesuffix(" []") + newline
            changed = True
    section = lines[plugin_idx:end]

    def bounds(name: str) -> tuple[int | None, int | None]:
        start = next((i for i, line in enumerate(section) if line.rstrip() == f"  {name}:"), None)
        if start is None:
            return None, None
        stop = next((i for i in range(start + 1, len(section)) if section[i].strip() and section[i].startswith("  ") and not section[i].startswith("    ")), len(section))
        return start, stop

    disabled_start, disabled_stop = bounds("disabled")
    if disabled_start is not None and disabled_stop is not None:
        original = section[disabled_start + 1 : disabled_stop]
        filtered = [line for line in original if line.strip() != f"- {PLUGIN}"]
        if filtered != original:
            section[disabled_start + 1 : disabled_stop] = filtered
            changed = True

    enabled_start, enabled_stop = bounds("enabled")
    present = enabled_start is not None and enabled_stop is not None and any(
        line.strip() == f"- {PLUGIN}" for line in section[enabled_start + 1 : enabled_stop]
    )
    if not present:
        if enabled_start is None:
            section[1:1] = ["  enabled:\n", entry]
        else:
            enabled_start, enabled_stop = bounds("enabled")
            assert enabled_stop is not None
            section.insert(enabled_stop, entry)
        changed = True
    lines[plugin_idx:end] = section
    updated = "".join(lines)
    return updated, changed or updated != text


def enable_existing_top_level_toolset_text(text: str) -> tuple[str, bool]:
    """Append the plugin toolset only when the profile already pins top-level toolsets."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == "toolsets:" and not line.startswith((" ", "\t"))), None)
    if start is None:
        return text, False
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))), len(lines))
    if any(line.strip() == "- gateway_restart" for line in lines[start + 1 : end]):
        return text, False
    lines.insert(end, "  - gateway_restart\n")
    return "".join(lines), True


def atomic_write(path: Path, content: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def install_plugin(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{PLUGIN}-", dir=str(destination.parent)))
    try:
        for name in ("plugin.yaml", "__init__.py"):
            shutil.copy2(source / name, staging / name)
        os.chmod(staging, 0o700)
        if destination.exists() or destination.is_symlink():
            destination.rename(destination.with_name(f"{destination.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--hermes-home", type=Path, default=Path("/Users/mutlupolatcan/.hermes"))
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    home = args.hermes_home.resolve()
    print(f"mode={'APPLY' if args.apply else 'DRY-RUN'} profiles={','.join(PROFILES)}")
    for profile in PROFILES:
        config = home / "profiles" / profile / "config.yaml"
        destination = home / "profiles" / profile / "plugins" / PLUGIN
        if not config.is_file():
            raise SystemExit(f"missing config: {config}")
        if not args.apply:
            print(f"PLAN {profile}: install+enable {PLUGIN}")
            continue
        install_plugin(source, destination)
        original = config.read_text(encoding="utf-8")
        updated, plugin_changed = enable_plugin_text(original)
        updated, toolset_changed = enable_existing_top_level_toolset_text(updated)
        changed = plugin_changed or toolset_changed
        if changed:
            shutil.copy2(config, config.with_suffix(f".yaml.bak-{PLUGIN}"))
            atomic_write(config, updated, config.stat().st_mode & 0o777)
        print(f"OK {profile}: plugin installed config={'updated' if changed else 'current'}")
    print("No other profile, credential, coordinator ledger, or gateway process was modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
