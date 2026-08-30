import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
PLUGIN_DIR = ROOT / "components" / "operations" / "gateway-restart-request"
PLUGIN_PATH = PLUGIN_DIR / "__init__.py"
INSTALLER_PATH = PLUGIN_DIR / "install_gateway_restart_request.py"


class FakeContext:
    def __init__(self):
        self.tools = {}
        self.hooks = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RestartRequestPluginTests(unittest.TestCase):
    def test_registers_narrow_tool_and_fail_closed_terminal_hook(self):
        plugin = load(PLUGIN_PATH, "gateway_restart_request")
        ctx = FakeContext()
        plugin.register(ctx)
        self.assertEqual(set(ctx.tools), {"request_gateway_restart"})
        schema = ctx.tools["request_gateway_restart"]["schema"]
        self.assertNotIn("requester", schema["parameters"]["properties"])
        self.assertEqual(schema["parameters"]["additionalProperties"], False)
        self.assertEqual(ctx.tools["request_gateway_restart"]["toolset"], "gateway_restart")
        self.assertIn("pre_tool_call", ctx.hooks)

    def test_tool_builds_request_internally_and_preserves_validated_schema(self):
        plugin = load(PLUGIN_PATH, "gateway_restart_request_tool")
        payload = {
            "task_id": "OPS-198-general-canary",
            "target_profile": "general",
            "artifact_path": "/tmp/artifact",
            "artifact_sha256": "a" * 64,
            "expected_version": "0.20.3",
            "expected_pid": 123,
            "rollback_path": "/tmp/rollback",
            "rollback_sha256": "b" * 64,
            "health_url": "http://127.0.0.1:8787/health",
            "semantic_canary": {"path": "status", "equals": "ok"},
            "dependency_task_id": None,
            "barrier": "activate-before-continue",
        }
        observed = {}

        def fake_run(argv, **kwargs):
            observed["argv"] = argv
            observed["payload"] = json.loads(Path(argv[3]).read_text())
            return mock.Mock(returncode=0, stdout='{"status":"queued"}\n', stderr="")

        with mock.patch.object(plugin.subprocess, "run", side_effect=fake_run), mock.patch.object(plugin, "_requirements_available", return_value=True):
            result = json.loads(plugin._request_gateway_restart(payload))
        self.assertEqual(result["status"], "queued")
        self.assertEqual(observed["argv"][2], "request")
        self.assertFalse(Path(observed["argv"][3]).exists())
        self.assertEqual(observed["payload"], payload)
        self.assertNotIn("requester", observed["payload"])

    def test_facade_rejects_scalar_json_and_passes_only_minimal_environment(self):
        plugin = load(PLUGIN_PATH, "gateway_restart_request_result_contract")
        observed = {}

        def fake_run(argv, **kwargs):
            observed["env"] = kwargs["env"]
            return mock.Mock(returncode=0, stdout='["queued"]\n', stderr="")

        with mock.patch.object(plugin.subprocess, "run", side_effect=fake_run), mock.patch.object(plugin, "_requirements_available", return_value=True):
            result = json.loads(plugin._request_gateway_restart({"task_id": "x"}))
        self.assertEqual(result["reason"], "restart_facade_failed")
        self.assertEqual(set(observed["env"]), {"HERMES_HOME", "HOME", "PATH"})

    def test_direct_model_restart_routes_are_blocked_but_unrelated_commands_are_allowed(self):
        plugin = load(PLUGIN_PATH, "gateway_restart_request_guard")
        blocked = (
            "hermes gateway restart",
            "/Users/u/.local/bin/hermes -p coder gateway restart",
            "launchctl kickstart -k gui/501/ai.hermes.gateway-general",
            "launchctl bootout gui/501/ai.hermes.gateway-coder",
            "launchctl kill SIGTERM gui/501/ai.hermes.gateway-general",
            "h=hermes; $h gateway restart",
            "kill -USR1 12345 # gateway",
            "python -c \"subprocess.run(['launchctl','kickstart','-k','gui/501/ai.hermes.gateway-general'])\"",
        )
        for command in blocked:
            with self.subTest(command=command):
                result = plugin._pre_tool_call(tool_name="terminal", args={"command": command})
                self.assertEqual(result["action"], "block")
                self.assertIn("request_gateway_restart", result["message"])
        allowed = (
            "hermes gateway status",
            "launchctl print gui/501/ai.hermes.gateway-general",
            "launchctl kickstart -k gui/501/ai.hermes.gateway-restart-coordinator",
        )
        for command in allowed:
            with self.subTest(command=command):
                self.assertIsNone(plugin._pre_tool_call(tool_name="terminal", args={"command": command}))

    def test_check_fn_requires_authorized_home_and_coordinator_facade(self):
        plugin = load(PLUGIN_PATH, "gateway_restart_request_check")
        with tempfile.TemporaryDirectory() as directory:
            facade = Path(directory) / "restartctl.py"
            facade.write_text("#!/usr/bin/env python3\n")
            with mock.patch.dict(os.environ, {"HERMES_HOME": "/Users/u/.hermes/profiles/general"}, clear=False), mock.patch.object(plugin, "RESTARTCTL", facade):
                self.assertTrue(plugin._requirements_available())
            with mock.patch.dict(os.environ, {"HERMES_HOME": "/Users/u/.hermes/profiles/writer"}, clear=False), mock.patch.object(plugin, "RESTARTCTL", facade):
                self.assertFalse(plugin._requirements_available())


class InstallerTests(unittest.TestCase):
    def test_installer_scope_is_exactly_general_and_coder(self):
        installer = load(INSTALLER_PATH, "gateway_restart_request_installer")
        self.assertEqual(installer.PROFILES, ("general", "coder"))

    def test_config_edit_is_idempotent_and_does_not_add_other_profiles(self):
        installer = load(INSTALLER_PATH, "gateway_restart_request_installer_edit")
        original = "plugins:\n  enabled:\n    - linear\n"
        updated, changed = installer.enable_plugin_text(original)
        self.assertTrue(changed)
        self.assertIn("    - gateway-restart-request\n", updated)
        self.assertEqual(installer.enable_plugin_text(updated), (updated, False))

    def test_config_edit_normalizes_empty_enabled_and_removes_disabled_entry(self):
        installer = load(INSTALLER_PATH, "gateway_restart_request_installer_lists")
        original = (
            "plugins:\n"
            "  enabled: []\n"
            "  disabled:\n"
            "    - gateway-restart-request\n"
            "    - other\n"
        )
        updated, changed = installer.enable_plugin_text(original)
        self.assertTrue(changed)
        self.assertEqual(updated.count("  enabled:"), 1)
        enabled, disabled = updated.split("  disabled:\n", 1)
        self.assertIn("    - gateway-restart-request\n", enabled)
        self.assertNotIn("gateway-restart-request", disabled)

    def test_config_edit_does_not_normalize_unrelated_enabled_key(self):
        installer = load(INSTALLER_PATH, "gateway_restart_request_installer_scope")
        original = "feature:\n  enabled: []\nplugins:\n  enabled:\n    - linear\n"
        updated, _ = installer.enable_plugin_text(original)
        self.assertIn("feature:\n  enabled: []\n", updated)

    def test_existing_top_level_toolset_is_extended_without_restricting_unpinned_profile(self):
        installer = load(INSTALLER_PATH, "gateway_restart_request_installer_toolset")
        pinned = "toolsets:\n  - hermes-cli\nagent: {}\n"
        updated, changed = installer.enable_existing_top_level_toolset_text(pinned)
        self.assertTrue(changed)
        self.assertIn("  - gateway_restart\n", updated)
        self.assertEqual(installer.enable_existing_top_level_toolset_text("agent: {}\n"), ("agent: {}\n", False))

    def test_apply_changes_only_general_and_coder_in_isolated_nine_profile_home(self):
        profiles = ("general", "assistant", "researcher", "coder", "writer", "producer", "marketing", "health", "finance")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            baseline = {}
            for profile in profiles:
                root = home / "profiles" / profile
                root.mkdir(parents=True)
                text = "toolsets:\n  - hermes-cli\nplugins:\n  enabled:\n    - linear\n" if profile == "general" else "plugins:\n  enabled:\n    - linear\n"
                (root / "config.yaml").write_text(text)
                baseline[profile] = text
            result = subprocess.run(
                ["python3", str(INSTALLER_PATH), "--apply", "--hermes-home", str(home)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for profile in profiles:
                config = (home / "profiles" / profile / "config.yaml").read_text()
                plugin = home / "profiles" / profile / "plugins" / "gateway-restart-request"
                if profile in {"general", "coder"}:
                    self.assertTrue(plugin.is_dir())
                    self.assertIn("gateway-restart-request", config)
                else:
                    self.assertFalse(plugin.exists())
                    self.assertEqual(config, baseline[profile])


if __name__ == "__main__":
    unittest.main()
