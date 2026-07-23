#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from honcho_codex_adapter.compat import contract_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Hermes private contract used by the adapter")
    parser.add_argument("--manifest", type=Path, default=ROOT / "compatibility" / "hermes-current.json")
    parser.add_argument("--write-manifest", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = contract_report()
    if args.write_manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {args.manifest}")
        return 0
    if not args.manifest.is_file():
        print(f"missing compatibility manifest: {args.manifest}", file=sys.stderr)
        return 2
    expected = json.loads(args.manifest.read_text(encoding="utf-8"))
    compatible = (
        report.get("schema_version") == expected.get("schema_version")
        and report.get("symbols") == expected.get("symbols")
        and report.get("runtime", {}).get("python", "").rsplit(".", 1)[0]
        == expected.get("runtime", {}).get("python", "").rsplit(".", 1)[0]
        and report.get("runtime", {}).get("openai", "").split(".", 1)[0]
        == expected.get("runtime", {}).get("openai", "").split(".", 1)[0]
    )
    runtime_changes = {
        key: {"expected": expected.get("runtime", {}).get(key), "actual": value}
        for key, value in report.get("runtime", {}).items()
        if value != expected.get("runtime", {}).get(key)
    }
    payload = {
        "compatible": compatible,
        "runtime_changes": runtime_changes,
        "expected": expected,
        "actual": report,
    }
    print(json.dumps(payload if args.json else {"compatible": compatible}, indent=2, sort_keys=True))
    return 0 if compatible else 1


if __name__ == "__main__":
    raise SystemExit(main())
