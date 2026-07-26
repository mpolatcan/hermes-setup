from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from honcho_codex_adapter.app import create_app
from honcho_codex_adapter.config import load_config
from test_adapter import FakeDriver


ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_canonical_config_loads_and_has_four_workload_routes(self):
        config = load_config(ROOT / "config" / "adapter.toml")
        self.assertEqual(config.server.host, "127.0.0.1")
        self.assertEqual(config.server.port, 18080)
        self.assertEqual(set(config.models), {
            "honcho-deriver",
            "honcho-summary",
            "honcho-dialectic",
            "honcho-dream",
        })
        self.assertEqual(config.admission.weights["dialectic"], 8)

    def test_environment_override_is_typed_and_secret_is_not_rendered(self):
        with patch.dict(
            os.environ,
            {"HONCHO_CODEX_QUEUE_CAPACITY": "12", "HONCHO_CODEX_ADAPTER_API_KEY": "never-render"},
        ):
            config = load_config(ROOT / "config" / "adapter.toml")
        self.assertEqual(config.admission.capacity, 12)
        self.assertNotIn("never-render", config.effective_json())
        self.assertNotIn("api_key", config.effective_json())

    def test_global_upstream_override_retargets_workload_routes(self) -> None:
        with patch.dict(os.environ, {"HONCHO_CODEX_UPSTREAM_MODEL": "gpt-candidate"}, clear=False):
            config = load_config(ROOT / "config" / "adapter.toml")
        self.assertEqual(config.upstream.model, "gpt-candidate")
        self.assertEqual({route.upstream_model for route in config.models.values()}, {"gpt-candidate"})

    def test_non_loopback_listener_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(
                """[server]
host = "0.0.0.0"
port = 18080
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "loopback-only"):
                load_config(path)

    def test_model_catalog_comes_from_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.toml"
            path.write_text(
                """[models."custom-local"]
upstream_model = "gpt-5.6-luna"
queue_class = "summary"
""",
                encoding="utf-8",
            )
            config = load_config(path)
            app = create_app(FakeDriver(), api_key="test-secret", config=config)
            with TestClient(app) as client:
                body = client.get(
                    "/v1/models", headers={"Authorization": "Bearer test-secret"}
                ).json()
        self.assertEqual([item["id"] for item in body["data"]], ["custom-local"])
        self.assertEqual(app.state.scheduler._model_classes["custom-local"], "summary")


if __name__ == "__main__":
    unittest.main()
