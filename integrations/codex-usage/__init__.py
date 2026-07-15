"""Telegram-visible /codex_usage command for official Codex usage limits."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "scripts" / "codex_usage.py"


def _codex_usage(_raw_args: str = "") -> str:
    try:
        result = subprocess.run(
            [str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return "Codex usage komutu zaman aşımına uğradı."
    except Exception as exc:
        return f"Codex usage çalıştırılamadı: {type(exc).__name__}: {exc}"

    output = (result.stdout or result.stderr or "").strip()
    return output or "Codex usage çıktısı boş döndü."


def register(ctx):
    ctx.register_command(
        "codex-usage",
        _codex_usage,
        description="Codex kullanım limitlerini göster",
    )
