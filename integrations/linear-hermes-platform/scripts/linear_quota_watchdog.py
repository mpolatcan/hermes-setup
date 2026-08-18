#!/usr/bin/env python3
"""Profile-local entry point for the read-only Linear quota watchdog."""

from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from quota_watchdog import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
