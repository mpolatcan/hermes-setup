from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

from src.cache.client import close_cache, init_cache
from src.dreamer.orchestrator import run_dream
from src.utils.agent_tools import create_tool_executor

from honcho_dream_effect_canary import (
    OBSERVED,
    OBSERVER,
    WORKSPACE_PREFIX,
    _cleanup,
    _documents,
    _result_content,
    _setup,
)


async def run() -> dict[str, Any]:
    workspace = WORKSPACE_PREFIX + "e2e-" + uuid.uuid4().hex[:12]
    report: dict[str, Any] = {"workspace": workspace, "steps": []}
    setup_complete = False

    try:
        await _setup(workspace)
        setup_complete = True
        report["steps"].append("workspace_and_peers_created")

        execute_tool = await create_tool_executor(
            workspace_name=workspace,
            observer=OBSERVER,
            observed=OBSERVED,
            session_name=None,
            current_messages=None,
            include_observation_ids=True,
            history_token_limit=1_000,
            agent_type="dreamer",
            parent_category="canary",
        )
        seed_contents = [
            "Sandbox source alpha: the peer prefers concise updates.",
            "Sandbox source beta: the peer requests explicit verification.",
            "Sandbox source gamma: disposable canaries must clean up their state.",
        ]
        seed_result = await execute_tool(
            "create_observations",
            {
                "observations": [
                    {"content": content, "level": "explicit"}
                    for content in seed_contents
                ]
            },
        )
        if "Created 3 observations" not in _result_content(seed_result):
            raise AssertionError(f"seed creation failed: {_result_content(seed_result)}")
        report["steps"].append("explicit_seeds_verified")

        result = await run_dream(
            workspace_name=workspace,
            observer=OBSERVER,
            observed=OBSERVED,
            dream_type="omni",
            trigger_reason="manual_canary",
            delay_reason="none",
            documents_since_last_dream_at_schedule=3,
            document_threshold=3,
        )
        if result is None:
            raise AssertionError("run_dream returned None")
        if not result.deduction_success or not result.induction_success:
            raise AssertionError(
                "specialist failure: "
                f"deduction={result.deduction_success} "
                f"induction={result.induction_success}"
            )
        if result.total_iterations < 2:
            raise AssertionError(
                f"expected at least two specialist iterations, got {result.total_iterations}"
            )
        if result.input_tokens <= 0 or result.output_tokens <= 0:
            raise AssertionError(
                "missing model usage: "
                f"input={result.input_tokens} output={result.output_tokens}"
            )

        documents = await _documents(workspace)
        explicit_count = sum(
            1
            for document in documents
            if document["level"] == "explicit" and document["deleted_at"] is None
        )
        if explicit_count != 3:
            raise AssertionError(
                f"explicit seed integrity failed: expected 3, got {explicit_count}"
            )

        report["steps"].append("real_run_dream_verified")
        report["result"] = {
            "run_id": result.run_id,
            "deduction_success": result.deduction_success,
            "induction_success": result.induction_success,
            "total_iterations": result.total_iterations,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "duration_ms": round(result.total_duration_ms, 3),
            "documents_total": len(documents),
            "explicit_documents": explicit_count,
            "derived_documents": sum(
                1
                for document in documents
                if document["level"] in {"deductive", "inductive"}
                and document["deleted_at"] is None
            ),
        }
        report["status"] = "passed"
        return report
    finally:
        if setup_complete:
            report["cleanup"] = await _cleanup(workspace)
            print(json.dumps(report, sort_keys=True))


async def main() -> dict[str, Any]:
    await init_cache()
    try:
        return await run()
    finally:
        await close_cache()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise
