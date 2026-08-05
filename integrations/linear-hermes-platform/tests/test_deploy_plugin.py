"""Tests for the atomic Linear plugin deployment helper."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_plugin.py"
ALLOWLIST = (
    "__init__.py",
    "adapter.py",
    "ledger.py",
    "linear_client.py",
    "oauth_store.py",
    "mcp_client.py",
    "outbound_policy.py",
    "outbound_ledger.py",
    "linear_tools.py",
    "plugin.yaml",
)
FIX_COMMIT = "2fc28f4cf80b55c7a6a5f8e03ffbbb9153dfc47c"
FIX_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "679723f859f0a8baeb74dcba11961e5d57a9e892597da54d3b9a810dffedb3ad",
    "ledger.py": "59012eb54e4032cf61f3b4bd7315114e2a9c09d9a15387d5dadea6ba892a80b1",
    "linear_client.py": "70bff1072ff39c28917ccd0f015985495565db3b0ddce5c9311cf84212469e99",
    "linear_tools.py": "eca26788b4d62866e06482dceca21dc30675cc54bba919b1495ac3a92e62abfb",
    "mcp_client.py": "3debd6bbc7ba7b6084d8bfb39045a0ed97f7a514266896f3e59bf1c0f6f0a2e7",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "outbound_ledger.py": "aa61090da20e580d12e0bd321b152dfc00123f478bde9c4954497f10c2d62b06",
    "outbound_policy.py": "963e81aa311766744a005c60aa96a59bb317e3a8f674168429feb3bedb04327d",
    "plugin.yaml": "68d6aae07ffb392f613d927719f479ffe70c5253575915c1ec5c06d90e30cd98",
}
GOVERNANCE_COMMIT = "f553c648988f870aa9de1bd8b34999c74ea05c6e"
GOVERNANCE_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "7d0355e3b381dcbac2e718ca0dc0f38fcfd9946a7fe0275178de587bb570be6a",
    "ledger.py": "59012eb54e4032cf61f3b4bd7315114e2a9c09d9a15387d5dadea6ba892a80b1",
    "linear_client.py": "44f52019888b93ce0b144b09570eaf10eaba5b2593b0f11efac4fd81e6bf1189",
    "linear_tools.py": "b02f477d6df4cfe18e93abc80ba5851dea4fc021b733ac44a18083f836da821c",
    "mcp_client.py": "3debd6bbc7ba7b6084d8bfb39045a0ed97f7a514266896f3e59bf1c0f6f0a2e7",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "outbound_ledger.py": "aa61090da20e580d12e0bd321b152dfc00123f478bde9c4954497f10c2d62b06",
    "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
    "plugin.yaml": "68d6aae07ffb392f613d927719f479ffe70c5253575915c1ec5c06d90e30cd98",
}
LEDGER_HARDENING_COMMIT = "bf12127eb2c91c2f49a82b5f4aedde2bd17365c7"
LEDGER_HARDENING_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "7d0355e3b381dcbac2e718ca0dc0f38fcfd9946a7fe0275178de587bb570be6a",
    "ledger.py": "59012eb54e4032cf61f3b4bd7315114e2a9c09d9a15387d5dadea6ba892a80b1",
    "linear_client.py": "44f52019888b93ce0b144b09570eaf10eaba5b2593b0f11efac4fd81e6bf1189",
    "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
    "mcp_client.py": "3debd6bbc7ba7b6084d8bfb39045a0ed97f7a514266896f3e59bf1c0f6f0a2e7",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
    "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
    "plugin.yaml": "68d6aae07ffb392f613d927719f479ffe70c5253575915c1ec5c06d90e30cd98",
}
MCP_SCHEMA_COMMIT = "ae223f9cf10c1c78fed949dcdec890582fc49610"
MCP_SCHEMA_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "7d0355e3b381dcbac2e718ca0dc0f38fcfd9946a7fe0275178de587bb570be6a",
    "ledger.py": "59012eb54e4032cf61f3b4bd7315114e2a9c09d9a15387d5dadea6ba892a80b1",
    "linear_client.py": "44f52019888b93ce0b144b09570eaf10eaba5b2593b0f11efac4fd81e6bf1189",
    "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
    "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
    "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
    "plugin.yaml": "68d6aae07ffb392f613d927719f479ffe70c5253575915c1ec5c06d90e30cd98",
}
OPS73_COMMIT = "5822ea28c36856f0ce8f244035dd489cc4a7ddda"
OPS73_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "1e1828faa7fdebc632d49fb505524dee598a510d969d659f5b53d62342845c61",
    "ledger.py": "910c1314a9c3489e370759f270487227af8f949bcd0d889959f9e6df8a2d0e88",
    "linear_client.py": "b1a7b1ab431af6c26d22337caa4cb70b5feef6fe886fa4fb7e0e67b1ad351158",
    "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
    "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
    "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
    "plugin.yaml": "819912eec91576605f1fe401ce69811b335586184b4a27d5a36aa01a5ab208fb",
}
HEALTH_VERSION_COMMIT = "c12d73119e230437faf01f0cddc294bc5f364185"
HEALTH_VERSION_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "ff9aa401976a0e41e22cdbf4604dcf7e7a292a9d0628bb30fabcb02ed9083c1c",
    "ledger.py": "910c1314a9c3489e370759f270487227af8f949bcd0d889959f9e6df8a2d0e88",
    "linear_client.py": "b1a7b1ab431af6c26d22337caa4cb70b5feef6fe886fa4fb7e0e67b1ad351158",
    "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
    "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
    "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
    "plugin.yaml": "819912eec91576605f1fe401ce69811b335586184b4a27d5a36aa01a5ab208fb",
}
LIVE_REVISION_COMMIT = "d63a1e441ba3ef98c0f593116cce317a0fb566c9"
LIVE_REVISION_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "f7695ea3d48c3f2cfe7e881467ca8211961a7f1e0b4db72e42e0956de8487a43",
    "ledger.py": "910c1314a9c3489e370759f270487227af8f949bcd0d889959f9e6df8a2d0e88",
    "linear_client.py": "7cc114f486cb99e37a1419b30abe315683931f83420e3d4d6da7e398128c5c92",
    "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
    "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
    "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
    "plugin.yaml": "819912eec91576605f1fe401ce69811b335586184b4a27d5a36aa01a5ab208fb",
}
AUDIT_COMPLETION_COMMIT = "498408a0a10082f2d1c7742f68059ffc5899b144"
AUDIT_COMPLETION_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "b9bce9511ecc4700165b160a6f33e98994dcefeea6d7d646045fc08503f6f41c",
    "ledger.py": "910c1314a9c3489e370759f270487227af8f949bcd0d889959f9e6df8a2d0e88",
    "linear_client.py": "7cc114f486cb99e37a1419b30abe315683931f83420e3d4d6da7e398128c5c92",
    "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
    "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
    "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
    "plugin.yaml": "819912eec91576605f1fe401ce69811b335586184b4a27d5a36aa01a5ab208fb",
}
SESSIONLESS_FENCE_COMMIT = "db7fa04992a9fd3ae5c18fd1e938726f05efd4cc"
SESSIONLESS_FENCE_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "cc89960a21e72b48e69c1b1b492e139c47d83aeaeaf53d31c2fff6b7f3dfc9fb",
    "ledger.py": "a9e1432cf2d3b3cda9f6d2d6579cfa4c2ae6c151b660803be247cbc03681d542",
    "linear_client.py": "7cc114f486cb99e37a1419b30abe315683931f83420e3d4d6da7e398128c5c92",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
    "outbound_policy.py": "a099d8a4fe68f579da91fe3311285d44409691265a07de02d5f5ad1cba1e2289",
    "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
    "linear_tools.py": "fd332aa1443b665a681cfdc01c916419f6c2bc8f92b287943fc3cfbbd9baebd0",
    "plugin.yaml": "299390e58eb8e4a00e7350a33ecf5dc8908786c375b5c8ccbad992736f119d93",
}
SEMANTIC_START_COMMIT = "87868f2d3fcb27541398df1671e6b6ea8698cf59"
SEMANTIC_START_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "cc89960a21e72b48e69c1b1b492e139c47d83aeaeaf53d31c2fff6b7f3dfc9fb",
    "ledger.py": "a9e1432cf2d3b3cda9f6d2d6579cfa4c2ae6c151b660803be247cbc03681d542",
    "linear_client.py": "bb995c1eeccf0a91cda57c48e3787dce575c26f10e3fa2c13ded80da19dab920",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
    "outbound_policy.py": "29e7f91c9ef0e7b302f369d6aea49f0d6137a281d57a6df20eec2e1594ae9e46",
    "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
    "linear_tools.py": "c1d5f920aff8b0df299728d2d8c621ecd517bac917372d913cee6da7b032bf08",
    "plugin.yaml": "299390e58eb8e4a00e7350a33ecf5dc8908786c375b5c8ccbad992736f119d93",
}
AGENTSESSION_CLOSURE_COMMIT = "2f9aaabcfb0a3d080a1078c1506a000a20024190"
AGENTSESSION_CLOSURE_MANIFEST = {
    "__init__.py": "7d5de2107c3de5f641b6678ab0beb3042e1bdf55c1be754fdd6d81ec6a9fd800",
    "adapter.py": "771c78c3e420dcc7667163794ceacd9dd026ffa74015fd2df2fe439cfcc750d5",
    "ledger.py": "ac00c13e3d62da2a81d2c6f89ea98a6405911886c3b848e8d3300735b0ee21d1",
    "linear_client.py": "91084e4ee0b83fdaa20260bc2cf0cab8b4ad944265cb3882349de733f97eee4a",
    "oauth_store.py": "d9c310b0da0f19ea66852dba8f0c4dd65c82edeb4b335f4960ab6e668c57fa58",
    "mcp_client.py": "81fe6bcbb4cec6bc0eb265d9b720d94cc3f75cbc73114984f468c291603ee0d9",
    "outbound_policy.py": "29e7f91c9ef0e7b302f369d6aea49f0d6137a281d57a6df20eec2e1594ae9e46",
    "outbound_ledger.py": "e1e5754e0aa2ee118658ac36ec6a0cd772d476976d7fc14eece78cd97841f293",
    "linear_tools.py": "c1d5f920aff8b0df299728d2d8c621ecd517bac917372d913cee6da7b032bf08",
    "plugin.yaml": "ad0c41f5c2e93a2a37b6ee379d48a0f7578791cf841651092caa86648be98881",
}


def load_helper():
    if not SCRIPT.exists():
        raise AssertionError("deploy_plugin.py does not exist")
    spec = importlib.util.spec_from_file_location("linear_deploy_plugin", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run(*args: str, cwd: Path) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


class DeployPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        source = self.repo / "integrations" / "linear-hermes-platform"
        source.mkdir(parents=True)
        for index, name in enumerate(ALLOWLIST):
            (source / name).write_text(f"reviewed-{index}\n", encoding="utf-8")
        run("git", "init", "-q", cwd=self.repo)
        run("git", "config", "user.email", "tests@example.invalid", cwd=self.repo)
        run("git", "config", "user.name", "Tests", cwd=self.repo)
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-qm", "fixture", cwd=self.repo)
        self.commit = run("git", "rev-parse", "HEAD", cwd=self.repo)
        self.manifest = {
            name: hashlib.sha256((f"reviewed-{index}\n").encode()).hexdigest()
            for index, name in enumerate(ALLOWLIST)
        }

        self.profiles = self.root / "profiles"
        self.target = self.profiles / "general" / "plugins" / "linear"
        self.target.mkdir(parents=True)
        (self.profiles / "general" / "state").mkdir()
        (self.target / "old.py").write_text("old-runtime\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_tool_result_contract_fix_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(helper.REVIEWED_MANIFESTS[FIX_COMMIT], FIX_MANIFEST)

    def test_semantic_start_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(
            helper.REVIEWED_MANIFESTS[SEMANTIC_START_COMMIT],
            SEMANTIC_START_MANIFEST,
        )

    def test_agentsession_closure_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(
            helper.REVIEWED_MANIFESTS[AGENTSESSION_CLOSURE_COMMIT],
            AGENTSESSION_CLOSURE_MANIFEST,
        )

    def test_human_final_acceptance_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(helper.REVIEWED_MANIFESTS[GOVERNANCE_COMMIT], GOVERNANCE_MANIFEST)

    def test_outbound_ledger_hardening_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(
            helper.REVIEWED_MANIFESTS[LEDGER_HARDENING_COMMIT],
            LEDGER_HARDENING_MANIFEST,
        )
        self.assertEqual(
            helper.REVIEWED_MANIFESTS[MCP_SCHEMA_COMMIT],
            MCP_SCHEMA_MANIFEST,
        )

    def test_ops73_closure_reconciliation_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(helper.REVIEWED_MANIFESTS[OPS73_COMMIT], OPS73_MANIFEST)

    def test_health_version_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(
            helper.REVIEWED_MANIFESTS[HEALTH_VERSION_COMMIT],
            HEALTH_VERSION_MANIFEST,
        )

    def test_live_revision_closure_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(
            helper.REVIEWED_MANIFESTS[LIVE_REVISION_COMMIT],
            LIVE_REVISION_MANIFEST,
        )

    def test_audit_completion_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(
            helper.REVIEWED_MANIFESTS[AUDIT_COMPLETION_COMMIT],
            AUDIT_COMPLETION_MANIFEST,
        )

    def test_sessionless_terminal_fence_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(
            helper.REVIEWED_MANIFESTS[SESSIONLESS_FENCE_COMMIT],
            SESSIONLESS_FENCE_MANIFEST,
        )

    def test_deploy_promotes_exact_allowlist_and_preserves_rollback(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}

        result = helper.deploy_reviewed(
            repo_root=self.repo,
            profiles_root=self.profiles,
            profile="general",
            commit=self.commit,
        )

        self.assertEqual(set(ALLOWLIST), {path.name for path in self.target.iterdir()})
        self.assertEqual(0o700, self.target.stat().st_mode & 0o777)
        for name in ALLOWLIST:
            self.assertEqual(0o600, (self.target / name).stat().st_mode & 0o777)
            self.assertEqual(self.manifest[name], hashlib.sha256((self.target / name).read_bytes()).hexdigest())
        rollback = Path(result["rollback_path"])
        self.assertTrue(rollback.is_dir())
        self.assertEqual(0o700, rollback.stat().st_mode & 0o777)
        self.assertEqual("old-runtime\n", (rollback / "old.py").read_text(encoding="utf-8"))
        self.assertEqual(self.commit, result["commit"])
        self.assertTrue(result["rollback_digest"])
    def test_interruption_after_backup_rename_restores_original_target(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}

        def interrupt() -> None:
            raise RuntimeError("injected interruption")

        with self.assertRaisesRegex(RuntimeError, "injected interruption"):
            helper.deploy_reviewed(
                repo_root=self.repo,
                profiles_root=self.profiles,
                profile="general",
                commit=self.commit,
                _after_backup_hook=interrupt,
            )

        self.assertTrue(self.target.is_dir())
        self.assertEqual("old-runtime\n", (self.target / "old.py").read_text(encoding="utf-8"))
    def test_dirty_repository_is_rejected_before_mutation(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(helper.DeploymentError, "not clean"):
            helper.deploy_reviewed(
                repo_root=self.repo,
                profiles_root=self.profiles,
                profile="general",
                commit=self.commit,
            )

        self.assertEqual("old-runtime\n", (self.target / "old.py").read_text(encoding="utf-8"))

    def test_unreviewed_commit_is_rejected_before_mutation(self) -> None:
        helper = load_helper()
        with self.assertRaisesRegex(helper.DeploymentError, "no reviewed"):
            helper.deploy_reviewed(
                repo_root=self.repo,
                profiles_root=self.profiles,
                profile="general",
                commit=self.commit,
            )
        self.assertTrue((self.target / "old.py").exists())

    def test_reviewed_hash_mismatch_is_rejected_before_promotion(self) -> None:
        helper = load_helper()
        bad = dict(self.manifest)
        bad["adapter.py"] = "0" * 64
        helper.REVIEWED_MANIFESTS = {self.commit: bad}

        with self.assertRaisesRegex(helper.DeploymentError, "source hash mismatch"):
            helper.deploy_reviewed(
                repo_root=self.repo,
                profiles_root=self.profiles,
                profile="general",
                commit=self.commit,
            )
        self.assertTrue((self.target / "old.py").exists())

    def test_symlink_target_is_rejected(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        shutil_target = self.root / "elsewhere"
        shutil_target.mkdir()
        for item in self.target.iterdir():
            item.unlink()
        self.target.rmdir()
        self.target.symlink_to(shutil_target, target_is_directory=True)

        with self.assertRaisesRegex(helper.DeploymentError, "non-symlink"):
            helper.deploy_reviewed(
                repo_root=self.repo,
                profiles_root=self.profiles,
                profile="general",
                commit=self.commit,
            )

    def test_post_promotion_failure_restores_original_and_preserves_candidate(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}

        def fail(_target: Path) -> None:
            raise RuntimeError("verification failure")

        with self.assertRaisesRegex(RuntimeError, "verification failure"):
            helper.deploy_reviewed(
                repo_root=self.repo,
                profiles_root=self.profiles,
                profile="general",
                commit=self.commit,
                _post_promote_hook=fail,
            )

        self.assertEqual("old-runtime\n", (self.target / "old.py").read_text(encoding="utf-8"))
        failed = list((self.target.parent).glob(".linear-failed-*"))
        self.assertEqual(1, len(failed))
        self.assertEqual(set(ALLOWLIST), {path.name for path in failed[0].iterdir()})

    def test_exact_rollback_restores_old_tree_and_preserves_failed_current(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        deployed = helper.deploy_reviewed(
            repo_root=self.repo,
            profiles_root=self.profiles,
            profile="general",
            commit=self.commit,
        )

        result = helper.rollback_exact(
            profiles_root=self.profiles,
            profile="general",
            rollback_path=Path(deployed["rollback_path"]),
            rollback_digest=deployed["rollback_digest"],
        )

        self.assertEqual("rolled_back", result["status"])
        self.assertEqual("old-runtime\n", (self.target / "old.py").read_text(encoding="utf-8"))
        failed = Path(result["failed_path"])
        self.assertEqual(set(ALLOWLIST), {path.name for path in failed.iterdir()})

    def test_wrong_rollback_digest_causes_no_mutation(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        deployed = helper.deploy_reviewed(
            repo_root=self.repo,
            profiles_root=self.profiles,
            profile="general",
            commit=self.commit,
        )

        with self.assertRaisesRegex(helper.DeploymentError, "does not match"):
            helper.rollback_exact(
                profiles_root=self.profiles,
                profile="general",
                rollback_path=Path(deployed["rollback_path"]),
                rollback_digest="0" * 64,
            )
        self.assertEqual(set(ALLOWLIST), {path.name for path in self.target.iterdir()})

    def test_invalid_profile_name_is_rejected(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        with self.assertRaisesRegex(helper.DeploymentError, "Profile name"):
            helper.deploy_reviewed(
                repo_root=self.repo,
                profiles_root=self.profiles,
                profile="../general",
                commit=self.commit,
            )

    def test_lock_contention_times_out_without_mutation(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        lock_path = self.profiles / "general" / "state" / "linear-plugin-deploy.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(helper.DeploymentError, "timed out"):
                helper.deploy_reviewed(
                    repo_root=self.repo,
                    profiles_root=self.profiles,
                    profile="general",
                    commit=self.commit,
                    lock_timeout=0.05,
                )
        finally:
            os.close(fd)
        self.assertTrue((self.target / "old.py").exists())
    def test_coordinates_are_durable_and_announced_before_target_mutation(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        announcements = []

        def announce(payload):
            self.assertTrue((self.target / "old.py").exists())
            self.assertFalse(Path(payload["rollback_path"]).exists())
            records = list((self.profiles / "general" / "state").glob("linear-plugin-deploy-*.json"))
            self.assertEqual(1, len(records))
            announcements.append(payload)

        helper.deploy_reviewed(
            repo_root=self.repo,
            profiles_root=self.profiles,
            profile="general",
            commit=self.commit,
            announce=announce,
        )
        self.assertEqual(1, len(announcements))

    def test_rollback_is_revalidated_after_lock_acquisition(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        deployed = helper.deploy_reviewed(
            repo_root=self.repo,
            profiles_root=self.profiles,
            profile="general",
            commit=self.commit,
        )
        rollback = Path(deployed["rollback_path"])
        original_acquire = helper._acquire_lock

        def acquire_then_tamper(state_fd, timeout):
            fd = original_acquire(state_fd, timeout)
            (rollback / "old.py").write_text("tampered-after-lock\n", encoding="utf-8")
            return fd

        helper._acquire_lock = acquire_then_tamper
        with self.assertRaisesRegex(helper.DeploymentError, "does not match"):
            helper.rollback_exact(
                profiles_root=self.profiles,
                profile="general",
                rollback_path=rollback,
                rollback_digest=deployed["rollback_digest"],
            )
        self.assertEqual(set(ALLOWLIST), {path.name for path in self.target.iterdir()})

    def test_failed_post_restore_verification_restores_previous_current(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        deployed = helper.deploy_reviewed(
            repo_root=self.repo,
            profiles_root=self.profiles,
            profile="general",
            commit=self.commit,
        )

        def corrupt(restored: Path) -> None:
            (restored / "old.py").write_text("corrupted-rollback\n", encoding="utf-8")

        with self.assertRaisesRegex(helper.DeploymentError, "does not match"):
            helper.rollback_exact(
                profiles_root=self.profiles,
                profile="general",
                rollback_path=Path(deployed["rollback_path"]),
                rollback_digest=deployed["rollback_digest"],
                _post_restore_hook=corrupt,
            )

        self.assertEqual(set(ALLOWLIST), {path.name for path in self.target.iterdir()})
        rejected = list(self.target.parent.glob(".linear-rollback-failed-*"))
        self.assertEqual(1, len(rejected))
        self.assertEqual("corrupted-rollback\n", (rejected[0] / "old.py").read_text(encoding="utf-8"))
    def test_sigterm_after_verified_does_not_roll_back(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}

        result = helper.deploy_reviewed(
            repo_root=self.repo,
            profiles_root=self.profiles,
            profile="general",
            commit=self.commit,
            _after_verified_hook=lambda: os.kill(os.getpid(), signal.SIGTERM),
        )

        self.assertEqual("verified", result["status"])
        self.assertEqual(set(ALLOWLIST), {path.name for path in self.target.iterdir()})

    def test_sigterm_between_rollback_renames_restores_previous_current(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        deployed = helper.deploy_reviewed(
            repo_root=self.repo,
            profiles_root=self.profiles,
            profile="general",
            commit=self.commit,
        )

        with self.assertRaisesRegex(helper.DeploymentError, "signal"):
            helper.rollback_exact(
                profiles_root=self.profiles,
                profile="general",
                rollback_path=Path(deployed["rollback_path"]),
                rollback_digest=deployed["rollback_digest"],
                _after_current_backup_hook=lambda: os.kill(os.getpid(), signal.SIGTERM),
            )

        self.assertEqual(set(ALLOWLIST), {path.name for path in self.target.iterdir()})
        self.assertTrue(Path(deployed["rollback_path"]).is_dir())
    def test_repeated_sigterm_during_rollback_recovery_is_swallowed(self) -> None:
        helper = load_helper()
        helper.REVIEWED_MANIFESTS = {self.commit: self.manifest}
        deployed = helper.deploy_reviewed(
            repo_root=self.repo,
            profiles_root=self.profiles,
            profile="general",
            commit=self.commit,
        )

        with self.assertRaisesRegex(helper.DeploymentError, "signal"):
            helper.rollback_exact(
                profiles_root=self.profiles,
                profile="general",
                rollback_path=Path(deployed["rollback_path"]),
                rollback_digest=deployed["rollback_digest"],
                _after_current_backup_hook=lambda: os.kill(os.getpid(), signal.SIGTERM),
                _during_recovery_hook=lambda: os.kill(os.getpid(), signal.SIGTERM),
            )

        self.assertEqual(set(ALLOWLIST), {path.name for path in self.target.iterdir()})
        self.assertTrue(Path(deployed["rollback_path"]).is_dir())


if __name__ == "__main__":
    unittest.main()
