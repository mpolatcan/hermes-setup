#!/usr/bin/env python3
"""Classify and explicitly apply hermes-setup's Hermes Agent mail patches."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import NamedTuple


class PatchStatus(NamedTuple):
    patch: str
    state: str
    subject: str


def _git(runtime_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(runtime_root), *args],
        capture_output=True,
        text=True,
    )


def _require_checkout(runtime_root: Path) -> None:
    if not runtime_root.is_absolute() or not runtime_root.is_dir():
        raise RuntimeError("runtime root must be an existing absolute directory")
    result = _git(runtime_root, "rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RuntimeError("runtime root must be a Git worktree")


def _subject(patch_path: Path) -> str:
    for line in patch_path.read_text(errors="replace").splitlines():
        if line.startswith("Subject: "):
            subject = line.removeprefix("Subject: ").strip()
            if subject.startswith("[PATCH] "):
                subject = subject.removeprefix("[PATCH] ")
            if subject:
                return subject
    raise RuntimeError(f"patch has no Subject header: {patch_path.name}")


def _subject_in_history(runtime_root: Path, subject: str) -> bool:
    result = _git(
        runtime_root,
        "log",
        "-1",
        "--format=%s",
        "--fixed-strings",
        f"--grep={subject}",
    )
    return result.returncode == 0 and result.stdout.strip() == subject


def classify_patch(runtime_root: Path, patch_path: Path) -> PatchStatus:
    _require_checkout(runtime_root)
    if not patch_path.is_file():
        raise RuntimeError(f"missing patch: {patch_path}")
    subject = _subject(patch_path)
    reverse = _git(runtime_root, "apply", "--check", "--reverse", str(patch_path))
    if reverse.returncode == 0:
        state = (
            "already-applied"
            if _subject_in_history(runtime_root, subject)
            else "upstreamed"
        )
        return PatchStatus(patch_path.name, state, subject)
    forward = _git(runtime_root, "apply", "--check", str(patch_path))
    if forward.returncode == 0:
        return PatchStatus(patch_path.name, "applicable", subject)
    return PatchStatus(patch_path.name, "conflict", subject)


def _patches(patch_dir: Path) -> list[Path]:
    if not patch_dir.is_absolute() or not patch_dir.is_dir():
        raise RuntimeError("patch directory must be an existing absolute directory")
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        raise RuntimeError("patch directory contains no .patch files")
    return patches


def manage(
    runtime_root: Path,
    patch_dir: Path,
    *,
    mode: str,
) -> list[PatchStatus]:
    _require_checkout(runtime_root)
    patches = _patches(patch_dir)
    if mode not in {"check", "apply"}:
        raise RuntimeError(f"unsupported mode: {mode}")
    if mode == "apply" and _git(runtime_root, "status", "--porcelain").stdout.strip():
        raise RuntimeError("refusing to apply patches to a dirty candidate worktree")

    statuses: list[PatchStatus] = []
    for patch_path in patches:
        status = classify_patch(runtime_root, patch_path)
        if mode == "apply" and status.state == "applicable":
            applied = _git(runtime_root, "apply", str(patch_path))
            if applied.returncode != 0:
                raise RuntimeError(f"failed to apply {patch_path.name}")
            status = PatchStatus(status.patch, "already-applied", status.subject)
        statuses.append(status)
    return statuses


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument(
        "--patch-dir",
        type=Path,
        default=repo_root / "patches" / "hermes-agent",
    )
    parser.add_argument("--mode", required=True, choices=("check", "apply"))
    args = parser.parse_args()
    try:
        statuses = manage(
            args.runtime_root.resolve(),
            args.patch_dir.resolve(),
            mode=args.mode,
        )
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2

    payload = {
        "ok": all(s.state in {"already-applied", "upstreamed"} for s in statuses),
        "mode": args.mode,
        "patches": [s._asdict() for s in statuses],
    }
    print(json.dumps(payload, indent=2))
    if any(s.state == "conflict" for s in statuses):
        return 2
    if any(s.state == "applicable" for s in statuses):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
