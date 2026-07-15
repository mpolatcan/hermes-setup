from __future__ import annotations

import importlib.util
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


usage = load(ROOT / "scripts" / "codex_usage.py", "codex_usage")
installer = load(ROOT / "install.py", "codex_usage_installer")


class UsageTests(unittest.TestCase):
    def test_renders_official_table_without_identifiers(self):
        now = int(time.time())
        data = {
            "rateLimits": {
                "planType": "prolite",
                "primary": {"windowDurationMins": 10080, "usedPercent": 13, "resetsAt": now + 3600},
            },
            "rateLimitResetCredits": {"availableCount": 4},
            "email": "must-not-appear@example.com",
        }
        rendered = usage.render_usage(data)
        self.assertIn("## Codex Usage — Official", rendered)
        self.assertIn("| Plan | `prolite` |", rendered)
        self.assertIn("| 7-day usage | %13 |", rendered)
        self.assertNotIn("must-not-appear", rendered)

    def test_binary_override(self):
        old = usage.os.environ.get("CODEX_BIN")
        try:
            usage.os.environ["CODEX_BIN"] = "/tmp/codex-test"
            self.assertEqual(usage._codex_binary(), "/tmp/codex-test")
        finally:
            if old is None:
                usage.os.environ.pop("CODEX_BIN", None)
            else:
                usage.os.environ["CODEX_BIN"] = old


class InstallerTests(unittest.TestCase):
    def test_preserves_existing_plugin_and_comments(self):
        original = "# before\nplugins:\n  enabled:\n    - linear\n# after\nagent:\n  max_turns: 1\n"
        updated, changed = installer.enable_plugin_text(original)
        self.assertTrue(changed)
        self.assertIn("    - linear\n    - codex-usage\n", updated)
        self.assertIn("# before", updated)
        self.assertIn("# after", updated)
        again, changed_again = installer.enable_plugin_text(updated)
        self.assertFalse(changed_again)
        self.assertEqual(updated, again)

    def test_adds_missing_plugins_section(self):
        updated, changed = installer.enable_plugin_text("agent:\n  max_turns: 1\n")
        self.assertTrue(changed)
        self.assertTrue(updated.endswith("plugins:\n  enabled:\n    - codex-usage\n"))


if __name__ == "__main__":
    unittest.main()
