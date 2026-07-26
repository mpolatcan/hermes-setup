from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cleanup_honcho_workspaces.py"


def load_module():
    spec = importlib.util.spec_from_file_location("cleanup_honcho_workspaces", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CleanupHonchoWorkspaceTests(unittest.TestCase):
    def test_allowlists_are_disjoint_and_exact(self):
        module = load_module()
        self.assertEqual(
            module.ACTIVE_WORKSPACES,
            {"polatcan-gaming", "polatcan-finance", "polatcan-health"},
        )
        self.assertEqual(
            module.LEGACY_WORKSPACES,
            {
                "hermes_general",
                "hermes_researcher",
                "hermes_assistant",
                "hermes_marketing",
                "hermes_coder",
                "hermes_writer",
                "hermes_producer",
                "hermes_finance",
                "hermes_health",
            },
        )
        self.assertTrue(module.ACTIVE_WORKSPACES.isdisjoint(module.LEGACY_WORKSPACES))

    def test_apply_requires_exact_confirmation(self):
        module = load_module()
        with self.assertRaises(SystemExit):
            module.require_confirmation("wrong")
        module.require_confirmation(module.CONFIRMATION)

    def test_validate_inventory_rejects_unknown_or_missing_workspaces(self):
        module = load_module()
        expected = module.ACTIVE_WORKSPACES | module.LEGACY_WORKSPACES
        module.validate_inventory(expected)
        with self.assertRaisesRegex(RuntimeError, "unexpected workspace inventory"):
            module.validate_inventory(expected | {"unknown"})
        with self.assertRaisesRegex(RuntimeError, "unexpected workspace inventory"):
            module.validate_inventory(expected - {"hermes_general"})

    def test_no_active_workspace_can_be_a_deletion_target(self):
        module = load_module()
        for workspace in module.LEGACY_WORKSPACES:
            module.validate_deletion_target(workspace)
        for workspace in module.ACTIVE_WORKSPACES:
            with self.assertRaisesRegex(RuntimeError, "protected workspace"):
                module.validate_deletion_target(workspace)

    def test_external_api_resources_are_identified_by_id(self):
        module = load_module()
        self.assertEqual(module.resource_ids([{"id": "a"}, {"id": "b"}]), ["a", "b"])
        with self.assertRaisesRegex(RuntimeError, "missing id"):
            module.resource_ids([{"name": "legacy-internal-field"}])


if __name__ == "__main__":
    unittest.main()
