from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from aiohttp import web

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from mcp_client import (  # noqa: E402
    EXPECTED_VENDOR_TOOL_NAMES,
    LinearMCPClient,
    LinearMCPError,
    MCPOutcomeUnknown,
)


class FakeStore:
    def __init__(self) -> None:
        self.token = "old-access"
        self.calls = []

    async def access_token(self, *, force_refresh=False, stale_token=None):
        self.calls.append((force_refresh, stale_token))
        if force_refresh:
            self.token = "new-access"
        return self.token


class LinearMCPClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = FakeStore()
        self.requests = []
        self.unauthorized_once = False
        self.mutation_status = 200
        self.tool_error = False
        self.sse = False
        self.duplicate_sse = False
        self.oversized_tool_result = False
        self.protocol_version = "2025-03-26"
        self.next_cursor_override = None
        self.envelope_mode = None
        self.tool_rpc_error = False
        self.protocol_headers = []
        self.delete_calls = 0
        self.session_404_once = False
        self.response_content_type = None
        self.session_id_header = "session-1"
        self.tool_session_id_header = None
        fields = {
            "get_user": {"query"},
            "get_issue": {
                "id", "includeRelations", "includeCustomerNeeds", "includeReleases",
            },
            "list_issues": {
                "team", "limit", "cursor", "orderBy", "query", "state", "assignee",
                "delegate", "project", "cycle", "label", "createdAt", "updatedAt",
                "includeArchived", "fields", "parentId", "priority", "release",
            },
            "save_issue": {
                "id", "title", "description", "team", "state", "assignee", "delegate",
                "project", "milestone", "cycle", "labels", "parentId", "priority",
                "estimate", "dueDate", "blocks", "blockedBy", "relatedTo", "removeBlocks",
                "removeBlockedBy", "removeRelatedTo", "addReleases", "duplicateOf", "links",
                "patch", "removeReleases", "setReleases", "slaBreachesAt", "slaType",
            },
            "save_comment": {
                "id", "issueId", "body", "documentId", "initiativeId", "milestoneId",
                "parentId", "projectId", "statusUpdateId", "statusUpdateType",
            },
        }
        array_fields = {
            "labels", "blocks", "blockedBy", "relatedTo",
            "removeBlocks", "removeBlockedBy", "removeRelatedTo",
        }
        integer_fields = set()
        number_fields = {"estimate", "limit", "priority"}
        boolean_fields = {"includeRelations", "includeArchived"}

        nullable_fields = {
            ("list_issues", "assignee"), ("save_issue", "assignee"),
            ("save_issue", "cycle"), ("save_issue", "delegate"),
            ("save_issue", "dueDate"), ("save_issue", "estimate"),
            ("save_issue", "parentId"), ("save_issue", "project"),
        }

        def property_schema(tool_name, field):
            if field in array_fields:
                return {"type": "array", "items": {"type": "string"}}
            if field in integer_fields:
                return {"type": "integer"}
            if field in number_fields:
                result: dict[str, object] = {"type": "number"}
            elif field in boolean_fields:
                result = {"type": "boolean"}
            else:
                result = {"type": "string"}
            if (tool_name, field) in nullable_fields:
                result = {"anyOf": [{"type": result["type"]}, {"type": "null"}]}
            if (tool_name, field) == ("get_issue", "includeRelations"):
                result["default"] = False
            elif (tool_name, field) == ("list_issues", "includeArchived"):
                result["default"] = True
            elif (tool_name, field) == ("list_issues", "limit"):
                result.update({"default": 50, "maximum": 250})
            elif (tool_name, field) == ("list_issues", "orderBy"):
                result.update({"default": "updatedAt", "enum": ["createdAt", "updatedAt"]})
            return result

        self.tools = [
            {
                "name": name,
                "inputSchema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        field: property_schema(name, field) for field in sorted(tool_fields)
                    },
                    **(
                        {"required": ["id"]} if name == "get_issue"
                        else {"required": ["query"]} if name == "get_user"
                        else {"required": ["body"]} if name == "save_comment"
                        else {}
                    ),
                },
            }
            for name, tool_fields in fields.items()
        ]
        self.tools.extend(
            {
                "name": name,
                "inputSchema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                },
            }
            for name in sorted(EXPECTED_VENDOR_TOOL_NAMES - set(fields))
        )
        self.tools_pages = None
        self.malformed_tool_result = False
        self.tool_result_text = '{"id":"actor-1","name":"Derya"}'
        self.empty_tool_content = False

        def rpc_response(body, *, headers=None):
            if self.envelope_mode == "omit_jsonrpc":
                body.pop("jsonrpc", None)
            elif self.envelope_mode == "both_null_error" and "result" in body:
                body["error"] = None
            elif self.envelope_mode == "boolean_id":
                body["id"] = True
            if self.sse:
                event = f"event: message\ndata: {json.dumps(body)}\n\n"
                return web.Response(
                    text=event + event if self.duplicate_sse else event,
                    content_type="text/event-stream",
                    headers=headers,
                )
            if self.response_content_type is not None:
                response_headers = dict(headers or {})
                response_headers["Content-Type"] = self.response_content_type
                return web.Response(
                    text=json.dumps(body),
                    headers=response_headers,
                )
            return web.json_response(body, headers=headers)

        async def handler(request: web.Request) -> web.Response:
            payload = await request.json()
            self.requests.append(payload)
            self.protocol_headers.append(request.headers.get("MCP-Protocol-Version"))
            if self.unauthorized_once and request.headers.get("Authorization") == "Bearer old-access":
                self.unauthorized_once = False
                return web.json_response({"error": "expired"}, status=401)
            method = payload.get("method")
            request_id = payload.get("id")
            if method not in {"initialize", "notifications/initialized"} and self.session_404_once:
                self.session_404_once = False
                return web.json_response({"error": "session expired"}, status=404)
            if method == "initialize":
                return rpc_response(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": self.protocol_version,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "linear", "version": "test"},
                        },
                    },
                    headers={"Mcp-Session-Id": self.session_id_header},
                )
            if method == "notifications/initialized":
                return web.Response(status=202)
            if method == "tools/list":
                if self.tools_pages is not None:
                    cursor = payload.get("params", {}).get("cursor")
                    index = int(cursor or 0)
                    result = {"tools": self.tools_pages[index]}
                    if index + 1 < len(self.tools_pages):
                        result["nextCursor"] = (
                            self.next_cursor_override
                            if self.next_cursor_override is not None
                            else str(index + 1)
                        )
                    return rpc_response(
                        {"jsonrpc": "2.0", "id": request_id, "result": result}
                    )
                return rpc_response(
                    {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools}}
                )
            if method == "tools/call":
                name = payload["params"]["name"]
                if self.tool_rpc_error:
                    return rpc_response(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32000, "message": "unknown commit state"},
                        }
                    )
                if name.startswith("save_") and self.mutation_status != 200:
                    return web.json_response({"error": "temporary"}, status=self.mutation_status)
                if self.malformed_tool_result:
                    return rpc_response(
                        {"jsonrpc": "2.0", "id": request_id, "result": {"content": "bad"}}
                    )
                return rpc_response(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "isError": self.tool_error,
                            "content": [] if self.empty_tool_content else [
                                {
                                    "type": "text",
                                    "text": (
                                        "x" * (1024 * 1024 + 1)
                                        if self.oversized_tool_result
                                        else self.tool_result_text
                                    ),
                                }
                            ],
                        },
                    },
                    headers=(
                        {"Mcp-Session-Id": self.tool_session_id_header}
                        if self.tool_session_id_header is not None
                        else None
                    ),
                )
            return web.json_response({"error": "unknown"}, status=400)

        async def delete_handler(_request: web.Request) -> web.Response:
            self.delete_calls += 1
            return web.Response(status=204)

        app = web.Application()
        app.router.add_post("/mcp", handler)
        app.router.add_delete("/mcp", delete_handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None and server.sockets
        port = server.sockets[0].getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{port}/mcp"

    def client(self) -> LinearMCPClient:
        return LinearMCPClient(self.store, endpoint=self.endpoint, allow_test_endpoint=True)

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def test_connect_validates_required_vendor_contract(self):
        client = self.client()
        try:
            await client.connect()
            self.assertEqual(client.session_id, "session-1")
            self.assertEqual(set(client.tool_schemas), {t["name"] for t in self.tools})
            self.assertEqual(
                [item["method"] for item in self.requests[:3]],
                ["initialize", "notifications/initialized", "tools/list"],
            )
        finally:
            await client.close()

    async def test_current_vendor_draft_2020_12_schema_is_accepted(self):
        for tool in self.tools:
            schema = tool.get("inputSchema") or {}
            if "$schema" in schema:
                schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        client = self.client()
        try:
            await client.connect()
            self.assertEqual(set(client.tool_schemas), {tool["name"] for tool in self.tools})
        finally:
            await client.close()

    async def test_non_required_tool_schema_uri_contract_fails_closed(self):
        cases = ("missing", "non_object", "legacy_draft_07", "unknown_uri")
        for case in cases:
            with self.subTest(case=case):
                target = next(tool for tool in self.tools if tool["name"] == "get_team")
                if case == "missing":
                    target.pop("inputSchema")
                elif case == "non_object":
                    target["inputSchema"] = []
                elif case == "legacy_draft_07":
                    target["inputSchema"]["$schema"] = (
                        "http://json-schema.org/draft-07/schema#"
                    )
                else:
                    target["inputSchema"]["$schema"] = "https://example.invalid/schema"
                client = self.client()
                try:
                    with self.assertRaisesRegex(LinearMCPError, "schema drift"):
                        await client.connect()
                finally:
                    await client.close()
                await self.asyncTearDown()
                await self.asyncSetUp()

    async def test_missing_required_tool_fails_closed(self):
        self.tools = [tool for tool in self.tools if tool["name"] != "save_comment"]
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "tool-name contract"):
                await client.connect()
        finally:
            await client.close()

    async def test_tools_list_pagination_collects_required_contract(self):
        self.tools_pages = [self.tools[:2], self.tools[2:]]
        client = self.client()
        try:
            await client.connect()
            self.assertEqual(set(client.tool_schemas), {tool["name"] for tool in self.tools})
            list_calls = [item for item in self.requests if item.get("method") == "tools/list"]
            self.assertEqual(len(list_calls), 2)
            self.assertEqual(list_calls[1]["params"], {"cursor": "1"})
        finally:
            await client.close()

    async def test_required_tool_schema_drift_fails_closed(self):
        self.tools[0]["inputSchema"]["properties"].pop("query")
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "schema drift"):
                await client.connect()
        finally:
            await client.close()

    async def test_required_tool_schema_type_and_requiredness_drift_fails_closed(self):
        cases = [
            ("get_issue", "id", {"type": "integer"}, None),
            ("get_issue", "id", {"type": ["string", "object"]}, None),
            ("get_issue", "id", {"type": "string", "enum": ["OPS-1"]}, None),
            ("save_issue", "labels", {"type": "array", "items": {"type": "object"}}, None),
            ("get_issue", None, None, []),
            ("list_issues", None, None, ["query"]),
        ]
        for tool_name, field, property_schema, required in cases:
            with self.subTest(tool_name=tool_name, field=field, required=required):
                target = next(tool for tool in self.tools if tool["name"] == tool_name)
                if field is not None:
                    target["inputSchema"]["properties"][field] = property_schema
                if required is not None:
                    target["inputSchema"]["required"] = required
                client = self.client()
                try:
                    with self.assertRaisesRegex(LinearMCPError, "schema drift"):
                        await client.connect()
                finally:
                    await client.close()
                await self.asyncTearDown()
                await self.asyncSetUp()

    async def test_added_vendor_schema_field_fails_closed(self):
        target = next(tool for tool in self.tools if tool["name"] == "save_issue")
        target["inputSchema"]["properties"]["newVendorField"] = {"type": "string"}
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "schema drift"):
                await client.connect()
        finally:
            await client.close()

    async def test_vendor_tool_name_set_is_exact(self):
        mutations = ("missing", "additional", "renamed")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                if mutation == "missing":
                    self.tools.pop()
                elif mutation == "additional":
                    self.tools.append({"name": "unexpected_vendor_tool", "inputSchema": {}})
                else:
                    target = next(
                        tool for tool in self.tools if tool["name"] not in {
                            "get_user", "get_issue", "list_issues", "save_issue", "save_comment"
                        }
                    )
                    target["name"] = "renamed_vendor_tool"
                client = self.client()
                try:
                    with self.assertRaisesRegex(LinearMCPError, "tool-name contract"):
                        await client.connect()
                finally:
                    await client.close()
                await self.asyncTearDown()
                await self.asyncSetUp()

    async def test_duplicate_or_oversized_tool_contract_fails_closed(self):
        self.tools.append(dict(self.tools[0]))
        client = self.client()
        try:
            with self.assertRaises(LinearMCPError):
                await client.connect()
        finally:
            await client.close()

        await self.asyncTearDown()
        await self.asyncSetUp()
        extras = [
            {
                "name": f"extra_{index}",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            }
            for index in range(600)
        ]
        self.tools.extend(extras)
        client = self.client()
        try:
            with self.assertRaises(LinearMCPError):
                await client.connect()
        finally:
            await client.close()

    async def test_cursor_type_is_rejected_before_followup_request(self):
        self.tools_pages = [self.tools[:2], self.tools[2:]]
        self.next_cursor_override = {"secret": "not-a-cursor"}
        client = self.client()
        try:
            with self.assertRaises(LinearMCPError):
                await client.connect()
            list_calls = [r for r in self.requests if r.get("method") == "tools/list"]
            self.assertEqual(len(list_calls), 1)
        finally:
            await client.close()

    async def test_jsonrpc_envelope_is_exact(self):
        for mode in ("omit_jsonrpc", "both_null_error", "boolean_id"):
            with self.subTest(mode=mode):
                self.envelope_mode = mode
                client = self.client()
                try:
                    with self.assertRaises(LinearMCPError):
                        await client.connect()
                finally:
                    await client.close()
                self.envelope_mode = None

    async def test_unexpected_response_content_type_fails_closed(self):
        self.response_content_type = "text/plain"
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "content type"):
                await client.connect()
        finally:
            await client.close()

    async def test_malformed_content_type_parameter_fails_closed(self):
        self.response_content_type = "application/json;invalid"
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "content type"):
                await client.connect()
        finally:
            await client.close()

    async def test_asymmetrically_quoted_charset_fails_closed(self):
        self.response_content_type = 'application/json;charset="utf-8'
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "content type"):
                await client.connect()
        finally:
            await client.close()

    async def test_mutation_rpc_or_tool_error_is_outcome_unknown(self):
        client = self.client()
        try:
            await client.connect()
            for attribute in ("tool_rpc_error", "tool_error"):
                with self.subTest(attribute=attribute):
                    setattr(self, attribute, True)
                    with self.assertRaises(MCPOutcomeUnknown):
                        await client.call_tool("save_issue", {"id": "OPS-1"}, mutation=True)
                    setattr(self, attribute, False)
        finally:
            await client.close()

    async def test_legacy_protocol_version_is_rejected(self):
        self.protocol_version = "2024-11-05"
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "unsupported protocol"):
                await client.connect()
        finally:
            await client.close()

    async def test_401_refreshes_once_and_preserves_identity(self):
        self.unauthorized_once = True
        client = self.client()
        try:
            await client.connect()
            result = await client.call_tool("get_user", {"query": "me"})
            self.assertEqual(result["content"][0]["text"], '{"id":"actor-1","name":"Derya"}')
            self.assertIn((True, "old-access"), self.store.calls)
        finally:
            await client.close()

    async def test_mutation_401_is_not_redispatched(self):
        client = self.client()
        try:
            await client.connect()
            self.requests.clear()
            self.unauthorized_once = True
            with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
            calls = [item for item in self.requests if item.get("method") == "tools/call"]
            self.assertEqual(len(calls), 1)
            self.assertIn((True, "old-access"), self.store.calls)
        finally:
            await client.close()

    async def test_mutation_5xx_is_unknown_and_not_retried(self):
        client = self.client()
        try:
            await client.connect()
            self.requests.clear()
            self.mutation_status = 503
            with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
            calls = [item for item in self.requests if item.get("method") == "tools/call"]
            self.assertEqual(len(calls), 1)
        finally:
            await client.close()

    async def test_rejects_non_official_endpoint_by_default(self):
        with self.assertRaisesRegex(LinearMCPError, "official endpoint"):
            LinearMCPClient(self.store, endpoint=self.endpoint)

    async def test_non_required_vendor_tool_cannot_be_called(self):
        client = LinearMCPClient(
            self.store,
            endpoint=self.endpoint,
            required_tools=EXPECTED_VENDOR_TOOL_NAMES,
            allow_test_endpoint=True,
        )
        client.tool_schemas = {"save_project": {"inputSchema": {}}}
        with self.assertRaisesRegex(LinearMCPError, "not authorized"):
            await client.call_tool("save_project", {"id": "project-1"}, mutation=True)

    async def test_mutation_classification_is_derived_from_tool_name(self):
        client = self.client()
        try:
            await client.connect()
            self.requests.clear()
            self.mutation_status = 503
            with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                await client.call_tool("save_issue", {"title": "safe-test"}, mutation=False)
            calls = [item for item in self.requests if item.get("method") == "tools/call"]
            self.assertEqual(len(calls), 1)
        finally:
            await client.close()

    async def test_sse_negotiated_protocol_and_session_delete(self):
        self.sse = True
        self.protocol_version = "2025-06-18"
        client = self.client()
        await client.connect()
        result = await client.call_tool("get_user", {"query": "me"})
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(client.protocol_version, "2025-06-18")
        self.assertEqual(self.protocol_headers[-1], "2025-06-18")
        await client.close()
        self.assertEqual(self.delete_calls, 1)

    async def test_session_id_with_non_visible_ascii_fails_closed(self):
        self.session_id_header = "invalid session"
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "session id"):
                await client.connect()
        finally:
            await client.close()

    async def test_invalid_initialize_envelope_does_not_commit_session_id(self):
        self.envelope_mode = "omit_jsonrpc"
        client = self.client()
        try:
            with self.assertRaises(LinearMCPError):
                await client.connect()
            self.assertIsNone(client.session_id)
        finally:
            await client.close()

    async def test_oversized_session_id_fails_closed(self):
        self.session_id_header = "x" * 1025
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "session id"):
                await client.connect()
        finally:
            await client.close()

    async def test_non_initialization_session_id_header_fails_closed(self):
        client = self.client()
        try:
            await client.connect()
            self.tool_session_id_header = "session-2"
            with self.assertRaisesRegex(LinearMCPError, "session id"):
                await client.call_tool("get_user", {"query": "me"})
            self.assertEqual(client.session_id, "session-1")
        finally:
            await client.close()

    async def test_mutation_session_id_header_is_outcome_unknown(self):
        client = self.client()
        try:
            await client.connect()
            self.tool_session_id_header = "session-2"
            with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
            self.assertEqual(client.session_id, "session-1")
        finally:
            await client.close()

    async def test_empty_mutation_session_header_is_outcome_unknown(self):
        client = self.client()
        try:
            await client.connect()
            self.tool_session_id_header = ""
            with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
            self.assertEqual(client.session_id, "session-1")
        finally:
            await client.close()

    async def test_duplicate_sse_or_oversized_mutation_response_is_unknown(self):
        client = self.client()
        try:
            await client.connect()
            self.sse = True
            self.duplicate_sse = True
            with self.assertRaises(MCPOutcomeUnknown):
                await client.call_tool("save_issue", {}, mutation=True)
            self.sse = False
            self.duplicate_sse = False
            self.oversized_tool_result = True
            with self.assertRaises(MCPOutcomeUnknown):
                await client.call_tool("save_issue", {}, mutation=True)
        finally:
            await client.close()

    async def test_mutation_tool_is_error_is_outcome_unknown(self):
        client = self.client()
        try:
            await client.connect()
            self.tool_error = True
            with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
        finally:
            await client.close()

    async def test_malformed_tool_result_fails_closed(self):
        client = self.client()
        try:
            await client.connect()
            self.malformed_tool_result = True
            with self.assertRaisesRegex(LinearMCPError, "result contract"):
                await client.call_tool("get_user", {"query": "me"})
        finally:
            await client.close()

    async def test_malformed_mutation_result_is_outcome_unknown(self):
        client = self.client()
        try:
            await client.connect()
            self.malformed_tool_result = True
            with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
        finally:
            await client.close()

    async def test_mutation_requires_authoritative_result_id(self):
        client = self.client()
        try:
            await client.connect()
            cases = [
                ("not-json", False),
                ('{"name":"missing-id"}', False),
                ('{"id":""}', False),
                ('{"id":"result-1"}', True),
            ]
            for text, accepted in cases:
                with self.subTest(text=text):
                    self.tool_result_text = text
                    if accepted:
                        result = await client.call_tool("save_issue", {}, mutation=True)
                        self.assertEqual(result["content"][0]["text"], text)
                    else:
                        with self.assertRaises(MCPOutcomeUnknown):
                            await client.call_tool("save_issue", {}, mutation=True)

            self.empty_tool_content = True
            with self.assertRaises(MCPOutcomeUnknown):
                await client.call_tool("save_comment", {}, mutation=True)
        finally:
            await client.close()

    async def test_read_recovers_one_lost_session(self):
        client = self.client()
        try:
            await client.connect()
            self.requests.clear()
            self.session_404_once = True
            result = await client.call_tool("get_user", {"query": "me"})
            self.assertEqual(result["content"][0]["type"], "text")
            tool_calls = [item for item in self.requests if item.get("method") == "tools/call"]
            self.assertEqual(len(tool_calls), 2)
            self.assertIn("initialize", [item.get("method") for item in self.requests])
        finally:
            await client.close()

    async def test_mutation_does_not_resend_after_lost_session(self):
        client = self.client()
        try:
            await client.connect()
            self.requests.clear()
            self.session_404_once = True
            with self.assertRaisesRegex(MCPOutcomeUnknown, "session was lost"):
                await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
            tool_calls = [item for item in self.requests if item.get("method") == "tools/call"]
            self.assertEqual(len(tool_calls), 1)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
