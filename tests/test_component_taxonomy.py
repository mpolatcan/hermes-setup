import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "components"

EXPECTED_COMPONENTS = {
    "platforms/linear-agent-platform",
    "secrets/1password-hermes-bootstrap",
    "memory/honcho-codex-bridge",
    "operations/gateway-restart-coordinator",
    "security/github-personal-ssh-guard",
    "commands/codex-usage-command",
}

FORBIDDEN_CATCH_ALLS = {"integrations", "misc", "utils", "helpers", "other"}
FORBIDDEN_OPAQUE_FILES = {
    "secrets/1password-hermes-bootstrap/scripts/hermes_gateway_sdk_bootstrap.py",
    "secrets/1password-hermes-bootstrap/scripts/hermes_desktop_keychain.sh",
    "secrets/1password-hermes-bootstrap/scripts/hermes_send_keychain.sh",
    "secrets/1password-hermes-bootstrap/scripts/hermes_serve_keychain.sh",
    "secrets/1password-hermes-bootstrap/scripts/install_candidate.sh",
    "security/github-personal-ssh-guard/install.py",
    "commands/codex-usage-command/install.py",
    "commands/codex-usage-command/scripts/codex_usage.py",
    "operations/gateway-restart-coordinator/restartctl.py",
    "operations/gateway-restart-coordinator/scripts/install.sh",
    "platforms/linear-agent-platform/scripts/deploy_plugin.py",
}


class ComponentTaxonomyTests(unittest.TestCase):
    def test_canonical_component_tree_has_only_named_domain_components(self) -> None:
        self.assertFalse((ROOT / "integrations").exists())
        actual = {
            str(path.relative_to(COMPONENTS))
            for domain in COMPONENTS.iterdir()
            if domain.is_dir()
            for path in domain.iterdir()
            if path.is_dir()
        }
        self.assertEqual(actual, EXPECTED_COMPONENTS)
        root_dirs = {path.name for path in ROOT.iterdir() if path.is_dir()}
        self.assertFalse(root_dirs & FORBIDDEN_CATCH_ALLS)

    def test_vendor_and_capability_are_explicit_in_component_and_tool_names(self) -> None:
        for relative in EXPECTED_COMPONENTS:
            domain, component = relative.split("/", 1)
            self.assertTrue(domain)
            self.assertIn("-", component, relative)

        bootstrap = COMPONENTS / "secrets/1password-hermes-bootstrap"
        source_tools = [path.name.lower() for path in bootstrap.rglob("*") if path.is_file()]
        self.assertTrue(source_tools)
        self.assertTrue(
            all(
                ("1password" in name or "onepassword" in name)
                for name in source_tools
                if name.endswith((".py", ".sh"))
            )
        )

        for relative in FORBIDDEN_OPAQUE_FILES:
            self.assertFalse((COMPONENTS / relative).exists(), relative)

    def test_component_catalog_documents_the_taxonomy_contract(self) -> None:
        catalog = (COMPONENTS / "README.md").read_text(encoding="utf-8")
        self.assertIn("components/<domain>/<vendor-or-product>-<capability>/", catalog)
        for relative in sorted(EXPECTED_COMPONENTS):
            self.assertIn(relative, catalog)


if __name__ == "__main__":
    unittest.main()
