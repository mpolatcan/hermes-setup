import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "manage_hermes_agent_patches.py"
STAGE_SCRIPT = (
    ROOT
    / "integrations"
    / "honcho-codex-adapter"
    / "scripts"
    / "stage_hermes_upgrade.sh"
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def load_module():
    spec = importlib.util.spec_from_file_location("patch_manager", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load patch manager")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HermesPatchManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / "candidate"
        self.patch_dir = base / "patches"
        self.repo.mkdir()
        self.patch_dir.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "target.txt").write_text("base\n")
        git(self.repo, "add", "target.txt")
        git(self.repo, "commit", "-qm", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "target.txt").write_text("base\nfeature\n")
        git(self.repo, "add", "target.txt")
        git(self.repo, "commit", "-qm", "fix(test): add feature")
        patch_text = git(self.repo, "format-patch", "-1", "--stdout") + "\n"
        self.patch_path = self.patch_dir / "0001-add-feature.patch"
        self.patch_path.write_text(patch_text)
        self.feature_sha = git(self.repo, "rev-parse", "HEAD")
        self.module = load_module()

    def test_classifies_applicable_patch(self):
        git(self.repo, "reset", "--hard", self.base_sha)
        status = self.module.classify_patch(self.repo, self.patch_path)
        self.assertEqual(status.state, "applicable")

    def test_classifies_applied_patch_when_subject_exists(self):
        status = self.module.classify_patch(self.repo, self.patch_path)
        self.assertEqual(status.state, "already-applied")

    def test_classifies_equivalent_upstream_change_without_patch_subject(self):
        git(self.repo, "reset", "--hard", self.base_sha)
        (self.repo / "target.txt").write_text("base\nfeature\n")
        git(self.repo, "add", "target.txt")
        git(self.repo, "commit", "-qm", "upstream implementation")
        status = self.module.classify_patch(self.repo, self.patch_path)
        self.assertEqual(status.state, "upstreamed")

    def test_classifies_conflict(self):
        git(self.repo, "reset", "--hard", self.base_sha)
        (self.repo / "target.txt").write_text("different\n")
        git(self.repo, "add", "target.txt")
        git(self.repo, "commit", "-qm", "incompatible change")
        status = self.module.classify_patch(self.repo, self.patch_path)
        self.assertEqual(status.state, "conflict")

    def test_apply_mode_applies_only_applicable_patch(self):
        git(self.repo, "reset", "--hard", self.base_sha)
        result = self.module.manage(self.repo, self.patch_dir, mode="apply")
        self.assertEqual([s.state for s in result], ["already-applied"])
        self.assertEqual((self.repo / "target.txt").read_text(), "base\nfeature\n")

    def test_apply_mode_rejects_dirty_candidate(self):
        git(self.repo, "reset", "--hard", self.base_sha)
        (self.repo / "untracked.txt").write_text("dirty\n")
        with self.assertRaisesRegex(RuntimeError, "dirty"):
            self.module.manage(self.repo, self.patch_dir, mode="apply")

    def test_stage_script_defaults_are_root_anchored(self) -> None:
        text = STAGE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('[[ $# -eq 4 ]] || usage', text)
        self.assertIn('config_path="$2"', text)
        self.assertIn('adapter_python="$3"', text)

    def test_stage_script_has_fail_closed_clean_official_sha_gate(self) -> None:
        text = STAGE_SCRIPT.read_text()
        self.assertIn('expected_sha="$4"', text)
        self.assertIn('candidate_head="$(git -C "$candidate_root" rev-parse HEAD)"', text)
        self.assertIn('[[ "$candidate_head" == "$expected_sha" ]]', text)
        self.assertIn("NousResearch/hermes-agent.git", text)
        self.assertIn('git -C "$candidate_root" diff --quiet --', text)
        self.assertIn('status --porcelain --untracked-files=all', text)
        self.assertNotIn("manage_hermes_agent_patches.py", text)


if __name__ == "__main__":
    unittest.main()
