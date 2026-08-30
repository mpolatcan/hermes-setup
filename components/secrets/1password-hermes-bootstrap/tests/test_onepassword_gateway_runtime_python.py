import unittest
from pathlib import Path


COMPONENT = Path(__file__).resolve().parents[1]
SCRIPT = COMPONENT / "scripts" / "install_onepassword_hermes_candidate.sh"
OFFICIAL_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"


class CandidateInstallerContractTests(unittest.TestCase):
    def test_candidate_installer_defaults_to_signed_psf_python(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(OFFICIAL_PYTHON, source)
        self.assertNotIn("/opt/homebrew/opt/python@3.13", source)

    def test_candidate_installer_references_existing_canonical_sources(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"$ROOT/requirements/onepassword-hermes-gateway-sdk.txt"', source)
        self.assertTrue(
            (COMPONENT / "requirements" / "onepassword-hermes-gateway-sdk.txt").is_file()
        )
        self.assertIn('"$ROOT/scripts/onepassword_hermes_gateway_sdk_bootstrap.py"', source)
        self.assertTrue(
            (COMPONENT / "scripts" / "onepassword_hermes_gateway_sdk_bootstrap.py").is_file()
        )


if __name__ == "__main__":
    unittest.main()
