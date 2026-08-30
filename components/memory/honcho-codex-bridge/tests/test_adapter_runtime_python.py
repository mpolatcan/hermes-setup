from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_runtime.sh"
OFFICIAL_PYTHON = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"


def test_adapter_deploy_uses_signed_psf_python_for_sdk_runtime() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert f'SDK_PYTHON="{OFFICIAL_PYTHON}"' in source
    assert "/opt/homebrew/bin/python3.13" not in source
