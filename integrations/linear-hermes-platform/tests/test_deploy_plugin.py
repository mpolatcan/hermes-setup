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

    def test_human_final_acceptance_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(helper.REVIEWED_MANIFESTS[GOVERNANCE_COMMIT], GOVERNANCE_MANIFEST)

    def test_outbound_ledger_hardening_commit_is_reviewed(self) -> None:
        helper = load_helper()
        self.assertEqual(
            helper.REVIEWED_MANIFESTS[LEDGER_HARDENING_COMMIT],
            LEDGER_HARDENING_MANIFEST,
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
