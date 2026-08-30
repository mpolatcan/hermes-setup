#!/usr/bin/env python3
"""Install the codex-usage plugin across the nine-profile Hermes fleet.

Dry-run by default. `--apply` writes plugin files, profile symlinks and narrowly
adds `codex-usage` to existing block-style `plugins.enabled` lists. It never
restarts gateways and never reads or copies credentials.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROFILES = ("general", "assistant", "researcher", "coder", "writer", "producer", "marketing", "health", "finance")
PLUGIN = "codex-usage"


def enable_plugin_text(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    plugin_idx = next((i for i, line in enumerate(lines) if line.rstrip() == "plugins:" and not line.startswith((" ", "\t"))), None)
    if plugin_idx is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        return text + suffix + "plugins:\n  enabled:\n    - codex-usage\n", True

    end = next((i for i in range(plugin_idx + 1, len(lines)) if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))), len(lines))
    section = lines[plugin_idx:end]
    if any(line.strip() == "- codex-usage" for line in section):
        return text, False

    enabled_rel = next((i for i, line in enumerate(section) if line.rstrip() == "  enabled:"), None)
    if enabled_rel is None:
        lines.insert(plugin_idx + 1, "  enabled:\n")
        lines.insert(plugin_idx + 2, "    - codex-usage\n")
    else:
        enabled_abs = plugin_idx + enabled_rel
        insert_at = enabled_abs + 1
        while insert_at < end:
            line = lines[insert_at]
            if line.startswith("    ") or not line.strip():
                insert_at += 1
                continue
            break
        lines.insert(insert_at, "    - codex-usage\n")
    return "".join(lines), True


def copy_plugin(source: Path, destination: Path) -> bool:
    source_to_runtime = (
        (Path("plugin.yaml"), Path("plugin.yaml")),
        (Path("__init__.py"), Path("__init__.py")),
        (Path("scripts/codex_usage_command.py"), Path("scripts/codex_usage.py")),
    )
    if destination.is_dir() and all(
        (destination / runtime_rel).exists()
        and (destination / runtime_rel).read_bytes() == (source / source_rel).read_bytes()
        for source_rel, runtime_rel in source_to_runtime
    ):
        return False

    staging = Path(tempfile.mkdtemp(prefix="codex-usage-", dir=str(destination.parent)))
    try:
        for name in ("plugin.yaml", "__init__.py"):
            shutil.copy2(source / name, staging / name)
        (staging / "scripts").mkdir()
        shutil.copy2(
            source / "scripts" / "codex_usage_command.py",
            staging / "scripts" / "codex_usage.py",
        )
        os.chmod(staging / "scripts" / "codex_usage.py", 0o755)
        if destination.exists() or destination.is_symlink():
            backup = destination.with_name(f"{destination.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            destination.rename(backup)
        staging.rename(destination)
        return True
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="apply changes; default is dry-run")
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--profiles", nargs="*", default=list(PROFILES))
    args = parser.parse_args()

    source = Path(__file__).resolve().parent
    required = (source / "plugin.yaml", source / "__init__.py", source / "scripts" / "codex_usage_command.py")
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("Missing source files: " + ", ".join(missing), file=sys.stderr)
        return 2

    home = args.hermes_home.expanduser().resolve()
    shared = home / "plugins" / PLUGIN
    print(f"mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"shared_plugin={shared}")
    print("profiles=" + ",".join(args.profiles))
    if not args.apply:
        for profile in args.profiles:
            print(f"PLAN {profile}: link plugins/{PLUGIN}; ensure config plugins.enabled contains {PLUGIN}")
        print("No gateway restart will be performed.")
        return 0

    shared.parent.mkdir(parents=True, exist_ok=True)
    plugin_changed = copy_plugin(source, shared)
    changed_configs = 0
    for profile in args.profiles:
        profile_dir = home / "profiles" / profile
        config = profile_dir / "config.yaml"
        if not config.exists():
            print(f"ERROR {profile}: missing {config}", file=sys.stderr)
            return 3
        plugins_dir = profile_dir / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        link = plugins_dir / PLUGIN
        if link.is_symlink() and link.resolve() == shared.resolve():
            pass
        else:
            if link.exists() or link.is_symlink():
                backup = link.with_name(f"{link.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                link.rename(backup)
            link.symlink_to(shared)

        old = config.read_text()
        new, changed = enable_plugin_text(old)
        if changed:
            shutil.copy2(config, config.with_suffix(".yaml.bak-codex-usage"))
            config.write_text(new)
            changed_configs += 1
        print(f"OK {profile}: link={link.resolve()} config={'updated' if changed else 'already-enabled'}")

    print(f"installed_profiles={len(args.profiles)} plugin={'updated' if plugin_changed else 'already-current'} changed_configs={changed_configs}")
    print("Restart required for newly loaded plugins, but this installer intentionally does not restart gateways.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
