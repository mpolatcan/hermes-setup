from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from honcho_codex_adapter.driver import build_upstream_kwargs
from honcho_codex_adapter.models import ChatCompletionRequest


DEFAULT_HONCHO_AGENT_TOOLS = Path(
    "/Users/mutlupolatcan/honcho-stack/server/src/utils/agent_tools.py"
)
CATALOG_NAMES = {
    "TOOLS",
    "DIALECTIC_TOOLS",
    "DIALECTIC_TOOLS_MINIMAL",
    "DREAMER_TOOLS",
    "DEDUCTION_SPECIALIST_TOOLS",
    "INDUCTION_SPECIALIST_TOOLS",
}
SCHEMA_HELPERS = {
    "_base_observation_properties",
    "_generic_observation_item_schema",
    "_deductive_observation_item_schema",
    "_inductive_observation_item_schema",
}


def load_honcho_tool_catalogs(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in SCHEMA_HELPERS:
            selected.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in CATALOG_NAMES:
                selected.append(node)
        elif isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & CATALOG_NAMES:
                selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {"Any": Any}
    exec(compile(module, str(path), "exec"), namespace)
    return {name: namespace[name] for name in CATALOG_NAMES}


def as_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


class HonchoCatalogContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured = os.getenv("HONCHO_AGENT_TOOLS_PATH")
        cls.path = Path(configured) if configured else DEFAULT_HONCHO_AGENT_TOOLS
        if not cls.path.exists():
            raise unittest.SkipTest(f"Honcho agent_tools.py not found: {cls.path}")
        cls.catalogs = load_honcho_tool_catalogs(cls.path)

    def test_every_current_honcho_tool_translates(self):
        tools = [as_openai_tool(tool) for tool in self.catalogs["TOOLS"].values()]
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-dream-deduction-luna",
            "messages": [{"role": "user", "content": "contract probe"}],
            "tools": tools,
            "tool_choice": "auto",
        })
        kwargs = build_upstream_kwargs(request, "catalog-contract")
        self.assertEqual(
            {tool["name"] for tool in kwargs["tools"]},
            set(self.catalogs["TOOLS"]),
        )

    def test_active_catalog_subsets_are_nonempty_and_valid(self):
        for catalog_name in CATALOG_NAMES - {"TOOLS"}:
            with self.subTest(catalog=catalog_name):
                catalog = self.catalogs[catalog_name]
                self.assertTrue(catalog)
                tools = [as_openai_tool(tool) for tool in catalog]
                request = ChatCompletionRequest.model_validate({
                    "model": "honcho-dialectic-luna",
                    "messages": [{"role": "user", "content": "catalog probe"}],
                    "tools": tools,
                    "tool_choice": "auto",
                })
                kwargs = build_upstream_kwargs(request, f"catalog-{catalog_name.lower()}")
                self.assertEqual(len(kwargs["tools"]), len(catalog))


if __name__ == "__main__":
    unittest.main()
