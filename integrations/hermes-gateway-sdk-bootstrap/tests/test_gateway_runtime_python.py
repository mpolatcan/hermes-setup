from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_candidate.sh"
OFFICIAL_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"


def test_candidate_installer_defaults_to_signed_psf_python() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert OFFICIAL_PYTHON in source
    assert "/opt/homebrew/opt/python@3.13" not in source
