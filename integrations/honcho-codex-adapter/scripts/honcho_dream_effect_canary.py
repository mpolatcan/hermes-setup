from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

from sqlalchemy import func, select

from src import crud, models, schemas
from src.cache.client import close_cache, init_cache
from src.dependencies import tracked_db
from src.utils.agent_tools import create_tool_executor

WORKSPACE_PREFIX = "codex-dream-effect-canary-"
OBSERVER = "canary-observer"
OBSERVED = "canary-observed"
PEER_CARD = [
    "IDENTITY: Name: Dream Effect Canary",
    "ATTRIBUTE: Scope: disposable sandbox only",
]


def _result_content(result: Any) -> str:
    return result.content if hasattr(result, "content") else str(result)


async def _setup(workspace: str) -> None:
    async with tracked_db("canary.setup") as db:
        workspace_result = await crud.get_or_create_workspace(
            db,
            schemas.WorkspaceCreate(name=workspace),
        )
        peers_result = await crud.get_or_create_peers(
            db,
            workspace,
            [schemas.PeerCreate(name=OBSERVER), schemas.PeerCreate(name=OBSERVED)],
        )
        await db.commit()
        await workspace_result.post_commit()
        await peers_result.post_commit()


async def _documents(workspace: str) -> list[dict[str, Any]]:
    async with tracked_db("canary.read_documents") as db:
        rows = (
            await db.execute(
                select(models.Document)
                .where(models.Document.workspace_name == workspace)
                .where(models.Document.observer == OBSERVER)
                .where(models.Document.observed == OBSERVED)
                .order_by(models.Document.created_at.asc(), models.Document.id.asc())
            )
        ).scalars().all()
        return [
            {
                "id": row.id,
                "content": row.content,
                "level": row.level,
                "source_ids": row.source_ids,
                "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
            }
            for row in rows
        ]


async def _peer_card(workspace: str) -> list[str] | None:
    async with tracked_db("canary.read_peer_card") as db:
        return await crud.get_peer_card(
            db,
            workspace_name=workspace,
            observer=OBSERVER,
            observed=OBSERVED,
        )


async def _cleanup(workspace: str) -> dict[str, Any]:
    if not workspace.startswith(WORKSPACE_PREFIX):
        raise RuntimeError(f"refusing cleanup outside sandbox prefix: {workspace}")

    async with tracked_db("canary.cleanup") as db:
        result = await crud.delete_workspace(db, workspace)

    async with tracked_db("canary.verify_cleanup") as db:
        workspace_count = int(
            await db.scalar(
                select(func.count(models.Workspace.id)).where(
                    models.Workspace.name == workspace
                )
            )
            or 0
        )
        document_count = int(
            await db.scalar(
                select(func.count(models.Document.id)).where(
                    models.Document.workspace_name == workspace
                )
            )
            or 0
        )
        peer_count = int(
            await db.scalar(
                select(func.count(models.Peer.id)).where(
                    models.Peer.workspace_name == workspace
                )
            )
            or 0
        )

    if (workspace_count, document_count, peer_count) != (0, 0, 0):
        raise AssertionError(
            "cleanup left rows: "
            f"workspace={workspace_count} documents={document_count} peers={peer_count}"
        )

    return {
        "workspace": workspace,
        "workspace_count": workspace_count,
        "document_count": document_count,
        "peer_count": peer_count,
        "cascade": {
            "peers_deleted": result.peers_deleted,
            "sessions_deleted": result.sessions_deleted,
            "messages_deleted": result.messages_deleted,
            "conclusions_deleted": result.conclusions_deleted,
        },
    }


async def run() -> dict[str, Any]:
    workspace = WORKSPACE_PREFIX + uuid.uuid4().hex[:12]
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
            "Sandbox delete target: this temporary fact must be removed.",
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

        seed_docs = await _documents(workspace)
        seed_by_content = {doc["content"]: doc for doc in seed_docs}
        if set(seed_by_content) != set(seed_contents):
            raise AssertionError(f"seed documents mismatch: {seed_docs}")
        report["steps"].append("explicit_seeds_verified")

        alpha_id = seed_by_content[seed_contents[0]]["id"]
        beta_id = seed_by_content[seed_contents[1]]["id"]
        delete_id = seed_by_content[seed_contents[2]]["id"]

        deductive_content = (
            "Sandbox deduction: concise updates should include explicit verification."
        )
        failed_write_result = await execute_tool(
            "create_observations_deductive",
            {
                "observations": [
                    {
                        "content": deductive_content,
                        "source_ids": [],
                        "premises": [seed_contents[0]],
                    }
                ]
            },
        )
        if "ERROR: All observations failed validation" not in _result_content(
            failed_write_result
        ):
            raise AssertionError(
                "invalid deductive write did not fail closed: "
                f"{_result_content(failed_write_result)}"
            )
        if len(await _documents(workspace)) != len(seed_docs):
            raise AssertionError("failed deductive write mutated persisted state")
        report["steps"].append("failed_write_zero_mutation_verified")

        deductive_result = await execute_tool(
            "create_observations_deductive",
            {
                "observations": [
                    {
                        "content": deductive_content,
                        "source_ids": [alpha_id],
                        "premises": [seed_contents[0]],
                    }
                ]
            },
        )
        if "1 deductive" not in _result_content(deductive_result):
            raise AssertionError(
                f"deductive creation failed: {_result_content(deductive_result)}"
            )

        inductive_content = (
            "Sandbox induction: the peer consistently values concise, verified work."
        )
        inductive_result = await execute_tool(
            "create_observations_inductive",
            {
                "observations": [
                    {
                        "content": inductive_content,
                        "source_ids": [alpha_id, beta_id],
                        "sources": [seed_contents[0], seed_contents[1]],
                        "pattern_type": "behavior",
                        "confidence": "low",
                    }
                ]
            },
        )
        if "1 inductive" not in _result_content(inductive_result):
            raise AssertionError(
                f"inductive creation failed: {_result_content(inductive_result)}"
            )

        documents_after_create = await _documents(workspace)
        by_content = {doc["content"]: doc for doc in documents_after_create}
        if by_content[deductive_content]["level"] != "deductive":
            raise AssertionError("deductive level was not persisted")
        if by_content[deductive_content]["source_ids"] != [alpha_id]:
            raise AssertionError("deductive source_ids were not persisted")
        if by_content[inductive_content]["level"] != "inductive":
            raise AssertionError("inductive level was not persisted")
        if by_content[inductive_content]["source_ids"] != [alpha_id, beta_id]:
            raise AssertionError("inductive source_ids were not persisted")
        if len(documents_after_create) != len(seed_docs) + 2:
            raise AssertionError("retry created an unexpected number of observations")
        report["steps"].append(
            "deductive_and_inductive_writes_with_retry_verified"
        )

        card_result = await execute_tool("update_peer_card", {"content": PEER_CARD})
        if "Updated peer card" not in _result_content(card_result):
            raise AssertionError(f"peer card update failed: {_result_content(card_result)}")
        if await _peer_card(workspace) != PEER_CARD:
            raise AssertionError("peer card did not persist exact content")
        repeated_card_result = await execute_tool(
            "update_peer_card", {"content": PEER_CARD}
        )
        if "Updated peer card" not in _result_content(repeated_card_result):
            raise AssertionError(
                f"peer card replay failed: {_result_content(repeated_card_result)}"
            )
        if await _peer_card(workspace) != PEER_CARD:
            raise AssertionError("peer card replay changed persisted state")
        report["steps"].append("peer_card_idempotent_update_verified")

        delete_result = await execute_tool(
            "delete_observations",
            {"observation_ids": [delete_id]},
        )
        if "Deleted 1 observations" not in _result_content(delete_result):
            raise AssertionError(f"delete failed: {_result_content(delete_result)}")
        deleted_doc = next(
            doc for doc in await _documents(workspace) if doc["id"] == delete_id
        )
        if deleted_doc["deleted_at"] is None:
            raise AssertionError("delete target was not soft-deleted")
        repeated_delete_result = await execute_tool(
            "delete_observations",
            {"observation_ids": [delete_id]},
        )
        if "Deleted 0 observations" not in _result_content(repeated_delete_result):
            raise AssertionError(
                "delete replay was not an idempotent no-op: "
                f"{_result_content(repeated_delete_result)}"
            )
        report["steps"].append("observation_idempotent_delete_verified")

        report["effect_counts"] = {
            "documents_total": len(await _documents(workspace)),
            "deductive_created": 1,
            "inductive_created": 1,
            "peer_card_entries": len(PEER_CARD),
            "observations_deleted": 1,
            "failed_write_mutations": 0,
            "idempotent_replays": 2,
        }
        report["status"] = "passed"
        return report
    finally:
        if setup_complete:
            cleanup = await _cleanup(workspace)
            report["cleanup"] = cleanup
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
