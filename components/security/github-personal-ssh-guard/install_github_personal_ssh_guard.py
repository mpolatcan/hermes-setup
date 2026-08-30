#!/usr/bin/env python3
"""Install the GitHub SSH transport guard across the nine-profile fleet.

Dry-run by default. ``--apply`` installs the shared plugin and SSH wrapper,
links/enables the plugin per profile, and adds the profile-shell
``GIT_SSH_COMMAND`` boundary. It never touches credentials or restarts gateways.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROFILES = (
    "general",
    "assistant",
    "researcher",
    "coder",
    "writer",
    "producer",
    "marketing",
    "health",
    "finance",
)
PLUGIN = "github-transport-guard"
BEGIN_MARKER = "# BEGIN HERMES GITHUB TRANSPORT GUARD"
END_MARKER = "# END HERMES GITHUB TRANSPORT GUARD"


def unsupported_plugin_list_lines(text: str) -> list[str]:
    lines = text.splitlines()
    plugin_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if line.rstrip() == "plugins:" and not line.startswith((" ", "\t"))
        ),
        None,
    )
    if plugin_idx is None:
        return []
    end = next(
        (
            i
            for i in range(plugin_idx + 1, len(lines))
            if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))
        ),
        len(lines),
    )
    unsupported: list[str] = []
    for line in lines[plugin_idx + 1 : end]:
        stripped = line.strip()
        if (
            stripped.startswith(("enabled:", "disabled:"))
            and stripped not in {"enabled:", "disabled:", "enabled: []", "disabled: []"}
        ):
            unsupported.append(stripped)
    return unsupported


def enable_plugin_text(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    flow_normalized = False
    for index, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if stripped in {"  enabled: []", "  disabled: []"}:
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = stripped.removesuffix(" []") + newline
            flow_normalized = True
    plugin_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if line.rstrip() == "plugins:" and not line.startswith((" ", "\t"))
        ),
        None,
    )
    entry = f"    - {PLUGIN}\n"
    if plugin_idx is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        return text + suffix + f"plugins:\n  enabled:\n{entry}", True

    end = next(
        (
            i
            for i in range(plugin_idx + 1, len(lines))
            if lines[i].strip() and not lines[i].startswith((" ", "\t", "#"))
        ),
        len(lines),
    )
    section = lines[plugin_idx:end]

    def subsection_range(name: str) -> tuple[int | None, int | None]:
        start = next(
            (i for i, line in enumerate(section) if line.rstrip() == f"  {name}:"),
            None,
        )
        if start is None:
            return None, None
        stop = next(
            (
                i
                for i in range(start + 1, len(section))
                if section[i].strip()
                and section[i].startswith("  ")
                and not section[i].startswith("    ")
            ),
            len(section),
        )
        return start, stop

    changed = flow_normalized
    disabled_start, disabled_stop = subsection_range("disabled")
    if disabled_start is not None and disabled_stop is not None:
        filtered = [
            line
            for line in section[disabled_start + 1 : disabled_stop]
            if line.strip() != f"- {PLUGIN}"
        ]
        original = section[disabled_start + 1 : disabled_stop]
        if filtered != original:
            section[disabled_start + 1 : disabled_stop] = filtered
            changed = True

    enabled_start, enabled_stop = subsection_range("enabled")
    enabled_has_plugin = (
        enabled_start is not None
        and enabled_stop is not None
        and any(
            line.strip() == f"- {PLUGIN}"
            for line in section[enabled_start + 1 : enabled_stop]
        )
    )
    if not enabled_has_plugin:
        if enabled_start is None:
            section[1:1] = ["  enabled:\n", entry]
        else:
            enabled_start, enabled_stop = subsection_range("enabled")
            assert enabled_stop is not None
            section.insert(enabled_stop, entry)
        changed = True

    lines[plugin_idx:end] = section
    updated = "".join(lines)
    return updated, changed


def enable_shell_guard_text(text: str, guard_path: str) -> tuple[str, bool]:
    block = (
        f"{BEGIN_MARKER}\n"
        f'export GIT_SSH_COMMAND="{guard_path}"\n'
        f"{END_MARKER}\n"
    )
    if BEGIN_MARKER in text or END_MARKER in text:
        start = text.find(BEGIN_MARKER)
        end = text.find(END_MARKER, start)
        if start < 0 or end < 0:
            raise ValueError("partial GitHub transport guard marker")
        end += len(END_MARKER)
        if end < len(text) and text[end] == "\n":
            end += 1
        updated = text[:start] + block + text[end:]
        return updated, updated != text
    suffix = "" if not text or text.endswith("\n") else "\n"
    return text + suffix + block, True


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def install_shared_plugin(source: Path, destination: Path) -> bool:
    files = ("plugin.yaml", "__init__.py")
    if destination.is_dir() and all(
        (destination / name).exists()
        and (destination / name).read_bytes() == (source / name).read_bytes()
        for name in files
    ):
        return False
    staging = Path(tempfile.mkdtemp(prefix=f"{PLUGIN}-", dir=str(destination.parent)))
    try:
        for name in files:
            shutil.copy2(source / name, staging / name)
            os.chmod(staging / name, 0o600)
        os.chmod(staging, 0o700)
        if destination.exists() or destination.is_symlink():
            backup = destination.with_name(
                f"{destination.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            destination.rename(backup)
        staging.rename(destination)
        return True
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def missing_profile_paths(home: Path, profiles: tuple[str, ...] | list[str]) -> list[str]:
    missing: list[str] = []
    for profile in profiles:
        profile_dir = home / "profiles" / profile
        for name in ("config.yaml", "init.sh"):
            path = profile_dir / name
            if not path.exists():
                missing.append(str(path))
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--hermes-home", type=Path, default=Path("/Users/mutlupolatcan/.hermes")
    )
    parser.add_argument("--profiles", nargs="*", default=list(PROFILES))
    args = parser.parse_args()

    source = Path(__file__).resolve().parent
    repo_root = source.parents[2]
    guard_source = repo_root / "scripts" / "hermes-agent-ssh-guard.sh"
    required = (source / "plugin.yaml", source / "__init__.py", guard_source)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("Missing source files: " + ", ".join(missing), file=sys.stderr)
        return 2

    home = args.hermes_home.expanduser().resolve()
    shared = home / "plugins" / PLUGIN
    guard = home / "scripts" / "hermes-agent-ssh-guard"
    print(f"mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print("profiles=" + ",".join(args.profiles))
    if not args.apply:
        for profile in args.profiles:
            print(
                f"PLAN {profile}: link+enable {PLUGIN}; set profile init GIT_SSH_COMMAND={guard}"
            )
        print("No credential change and no gateway restart will be performed.")
        return 0

    missing_profiles = missing_profile_paths(home, args.profiles)
    if missing_profiles:
        print("Missing profile files: " + ", ".join(missing_profiles), file=sys.stderr)
        return 3
    unsupported_configs: list[str] = []
    for profile in args.profiles:
        config = home / "profiles" / profile / "config.yaml"
        unsupported = unsupported_plugin_list_lines(config.read_text())
        if unsupported:
            unsupported_configs.append(f"{config}: {', '.join(unsupported)}")
    if unsupported_configs:
        print(
            "Unsupported flow-style plugin lists: " + "; ".join(unsupported_configs),
            file=sys.stderr,
        )
        return 4

    shared.parent.mkdir(parents=True, exist_ok=True)
    shared_changed = install_shared_plugin(source, shared)
    atomic_write(guard, guard_source.read_text(), 0o700)
    changed_configs = 0
    changed_inits = 0
    for profile in args.profiles:
        profile_dir = home / "profiles" / profile
        config = profile_dir / "config.yaml"
        init = profile_dir / "init.sh"
        plugins_dir = profile_dir / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        link = plugins_dir / PLUGIN
        if not (link.is_symlink() and link.resolve() == shared.resolve()):
            if link.exists() or link.is_symlink():
                link.rename(
                    link.with_name(f"{link.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                )
            link.symlink_to(shared)

        config_text, config_changed = enable_plugin_text(config.read_text())
        if config_changed:
            shutil.copy2(config, config.with_suffix(f".yaml.bak-{PLUGIN}"))
            atomic_write(config, config_text, config.stat().st_mode & 0o777)
            changed_configs += 1
        init_text, init_changed = enable_shell_guard_text(init.read_text(), str(guard))
        if init_changed:
            shutil.copy2(init, init.with_suffix(f".sh.bak-{PLUGIN}"))
            atomic_write(init, init_text, init.stat().st_mode & 0o777)
            changed_inits += 1
        print(
            f"OK {profile}: plugin={'updated' if config_changed else 'enabled'} "
            f"shell_guard={'updated' if init_changed else 'current'}"
        )

    print(
        f"installed_profiles={len(args.profiles)} shared_plugin="
        f"{'updated' if shared_changed else 'current'} changed_configs={changed_configs} "
        f"changed_inits={changed_inits}"
    )
    print("Gateway restart is required to load the hook; shell guard applies to fresh terminal calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
