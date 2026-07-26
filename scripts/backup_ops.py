#!/usr/bin/env python3
"""Shared verification and retention primitives for Hermes/Honcho backups."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable


def secure_directory(path: Path | str) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def secure_file(path: Path | str) -> Path:
    path = Path(path)
    path.chmod(0o600)
    return path


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256_manifest(path: Path | str) -> Path:
    path = Path(path)
    manifest = path.with_name(path.name + ".sha256")
    tmp = manifest.with_name("." + manifest.name + ".partial")
    tmp.write_text(f"{sha256_file(path)}  {path.name}\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, manifest)
    manifest.chmod(0o600)
    return manifest


def verify_gzip(path: Path | str) -> None:
    path = Path(path)
    try:
        with gzip.open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                pass
    except (OSError, EOFError) as exc:
        raise ValueError(f"invalid gzip archive: {path}: {exc}") from exc


def _verify_sqlite(path: Path) -> None:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    for attempt in range(3):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                rows = conn.execute("PRAGMA quick_check").fetchall()
            finally:
                conn.close()
            if rows != [("ok",)]:
                raise ValueError(f"SQLite quick_check failed for {path}: {rows[:5]}")
            return
        except sqlite3.OperationalError as exc:
            if attempt == 2:
                raise ValueError(f"SQLite open/check failed for {path}: {exc}") from exc
            time.sleep(0.5 * (attempt + 1))


def verify_hermes_zip(path: Path | str) -> dict:
    path = Path(path)
    if not zipfile.is_zipfile(path):
        raise ValueError(f"not a zip archive: {path}")
    sqlite_files = 0
    sqlite_ok = 0
    with zipfile.ZipFile(path, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"zip CRC failed: {bad}")
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if not names:
            raise ValueError("empty Hermes archive")
        if not any(Path(name).name in {"config.yaml", ".env", "state.db"} for name in names):
            raise ValueError("archive has no Hermes marker file")
        with tempfile.TemporaryDirectory(dir=path.parent) as temp_dir:
            temp_root = Path(temp_dir)
            for name in names:
                if not name.endswith(".db"):
                    continue
                sqlite_files += 1
                target = temp_root / f"db-{sqlite_files}.db"
                with archive.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                _verify_sqlite(target)
                sqlite_ok += 1
    return {
        "archive": str(path),
        "files": len(names),
        "sqlite_files": sqlite_files,
        "sqlite_ok": sqlite_ok,
        "sha256": sha256_file(path),
    }


def verify_snapshot(path: Path | str) -> dict:
    path = Path(path)
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"snapshot manifest missing: {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failed = manifest.get("failed_dbs") or []
    oversized = manifest.get("oversized_skipped") or []
    if failed or oversized:
        raise ValueError(f"incomplete snapshot: failed_dbs={failed}, oversized={oversized}")
    checked = 0
    for relative, expected_size in (manifest.get("files") or {}).items():
        candidate = path / relative
        if not candidate.is_file():
            raise ValueError(f"snapshot file missing: {relative}")
        if candidate.stat().st_size != expected_size:
            raise ValueError(f"snapshot size mismatch: {relative}")
        if candidate.suffix == ".db":
            _verify_sqlite(candidate)
            checked += 1
    return {"snapshot": str(path), "files": len(manifest.get("files") or {}), "sqlite_ok": checked}


def prune_to_count(paths: Iterable[Path | str], keep: int) -> list[Path]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    candidates = sorted((Path(path) for path in paths if Path(path).exists()), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for path in candidates[keep:]:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
            for suffix in (".sha256", ".meta.json"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
        removed.append(path)
    return removed


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("secure-dir", "secure-file", "write-sha256", "verify-gzip", "verify-hermes-zip", "verify-snapshot"):
        item = sub.add_parser(command)
        item.add_argument("path")
    prune = sub.add_parser("prune")
    prune.add_argument("pattern")
    prune.add_argument("--keep", type=int, required=True)
    snapshots = sub.add_parser("prune-snapshots")
    snapshots.add_argument("root")
    snapshots.add_argument("--keep", type=int, required=True)
    args = parser.parse_args()

    if args.command == "secure-dir":
        _print({"path": str(secure_directory(args.path)), "mode": "0700"})
    elif args.command == "secure-file":
        _print({"path": str(secure_file(args.path)), "mode": "0600"})
    elif args.command == "write-sha256":
        _print({"manifest": str(write_sha256_manifest(args.path))})
    elif args.command == "verify-gzip":
        verify_gzip(args.path)
        _print({"archive": args.path, "gzip": "ok"})
    elif args.command == "verify-hermes-zip":
        _print(verify_hermes_zip(args.path))
    elif args.command == "verify-snapshot":
        _print(verify_snapshot(args.path))
    elif args.command == "prune":
        paths = list(Path().glob(args.pattern)) if not os.path.isabs(args.pattern) else list(Path(args.pattern).parent.glob(Path(args.pattern).name))
        _print({"removed": [str(p) for p in prune_to_count(paths, args.keep)]})
    elif args.command == "prune-snapshots":
        root = Path(args.root)
        candidates = [p for p in root.iterdir() if p.is_dir()] if root.exists() else []
        _print({"removed": [str(p) for p in prune_to_count(candidates, args.keep)]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
