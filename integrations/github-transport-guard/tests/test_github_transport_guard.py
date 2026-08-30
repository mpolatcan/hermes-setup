import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "integrations" / "github-transport-guard"
PLUGIN_PATH = PLUGIN_DIR / "__init__.py"
INSTALLER_PATH = PLUGIN_DIR / "install.py"
GUARD_PATH = ROOT / "scripts" / "hermes-agent-ssh-guard.sh"


class FakeContext:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def load_plugin():
    spec = importlib.util.spec_from_file_location("github_transport_guard", PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_installer():
    spec = importlib.util.spec_from_file_location("github_transport_guard_installer", INSTALLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GitHubTransportGuardTests(unittest.TestCase):
    def test_blocks_direct_github_ssh_terminal_routes(self):
        plugin = load_plugin()
        commands = (
            "git clone git@github.com:mpolatcan/hermes-setup.git",
            "git fetch ssh://git@github.com/mpolatcan/hermes-setup.git",
            "ssh -T git@github.com",
            "ssh -p 443 git@ssh.github.com",
            "scp file git@github.com:owner/repo",
            "true https://x;ssh -l git github.com",
        )
        for command in commands:
            with self.subTest(command=command):
                result = plugin._pre_tool_call(tool_name="terminal", args={"command": command})
                self.assertEqual(result["action"], "block")
                self.assertIn("brokered HTTPS", result["message"])

    def test_blocks_github_ssh_nested_inside_execute_code(self):
        plugin = load_plugin()
        result = plugin._pre_tool_call(
            tool_name="execute_code",
            args={"code": "terminal('git ls-remote git@github.com:owner/repo.git')"},
        )
        self.assertEqual(result["action"], "block")

    def test_allows_https_and_unrelated_ssh(self):
        plugin = load_plugin()
        allowed = (
            ("terminal", {"command": "git ls-remote https://github.com/mpolatcan/hermes-setup.git"}),
            ("terminal", {"command": "git ls-remote https://git@github.com/mpolatcan/hermes-setup.git"}),
            ("terminal", {"command": "ssh build.internal.example"}),
            ("read_file", {"path": "/tmp/github.com-not-a-command"}),
        )
        for tool_name, args in allowed:
            with self.subTest(tool_name=tool_name, args=args):
                self.assertIsNone(plugin._pre_tool_call(tool_name=tool_name, args=args))

    def test_registers_fail_closed_pre_tool_hook(self):
        plugin = load_plugin()
        context = FakeContext()
        plugin.register(context)
        self.assertIs(context.hooks["pre_tool_call"], plugin._pre_tool_call)

    def test_git_ssh_guard_denies_every_git_ssh_transport(self):
        for host in ("git@github.com", "git@GITHUB.COM", "git@github.com.", "git@ssh.github.com", "git@build.internal.example"):
            with self.subTest(host=host):
                result = subprocess.run(
                    ["/bin/bash", str(GUARD_PATH), "-G", host],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 69)
                self.assertEqual(result.stdout, "")
                self.assertIn("GitHub SSH is disabled", result.stderr)

    def test_git_ssh_guard_rejects_resolved_github_alias(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("Host gh-alias\n  HostName github.com\n  User git\n")
            config_path = handle.name
        try:
            result = subprocess.run(
                ["/bin/bash", str(GUARD_PATH), "-G", "-F", config_path, "gh-alias"],
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            Path(config_path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 69)
        self.assertIn("GitHub SSH is disabled", result.stderr)

    def test_manifest_uses_supported_standalone_kind(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text()
        self.assertIn("kind: standalone\n", manifest)
        self.assertNotIn("kind: utility", manifest)

    def test_installer_updates_enabled_list_and_shell_boundary_idempotently(self):
        installer = load_installer()
        config = (
            "plugins:\n"
            "  enabled:\n"
            "    - linear\n"
            "  disabled:\n"
            "    - github-transport-guard\n"
        )
        updated, changed = installer.enable_plugin_text(config)
        self.assertTrue(changed)
        enabled_text, disabled_text = updated.split("  disabled:\n", 1)
        self.assertIn("    - github-transport-guard\n", enabled_text)
        self.assertNotIn("github-transport-guard", disabled_text)
        self.assertEqual(installer.enable_plugin_text(updated), (updated, False))

        init = "#!/bin/bash\nexport PATH=/usr/bin:/bin\n"
        guarded, changed = installer.enable_shell_guard_text(init, "/tmp/hermes-agent-ssh-guard")
        self.assertTrue(changed)
        self.assertIn('export GIT_SSH_COMMAND="/tmp/hermes-agent-ssh-guard"', guarded)
        self.assertEqual(installer.enable_shell_guard_text(guarded, "/tmp/hermes-agent-ssh-guard"), (guarded, False))

    def test_installer_rejects_flow_style_plugin_lists(self):
        installer = load_installer()
        self.assertEqual(
            installer.unsupported_plugin_list_lines(
                "plugins:\n  enabled: []\n  disabled: [github-transport-guard]\n"
            ),
            ["disabled: [github-transport-guard]"],
        )

    def test_installer_preflights_every_profile_before_writes(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "profiles" / "general").mkdir(parents=True)
            (home / "profiles" / "general" / "config.yaml").write_text("plugins: {}\n")
            missing = installer.missing_profile_paths(home, ("general", "missing"))
        self.assertEqual(
            missing,
            [
                str(home / "profiles" / "general" / "init.sh"),
                str(home / "profiles" / "missing" / "config.yaml"),
                str(home / "profiles" / "missing" / "init.sh"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
