from __future__ import annotations

import asyncio
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
    INITIAL_PROTOCOL_VERSION,
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
        self.initialize_capabilities = {"tools": {}}
        self.initialize_server_info = {"name": "linear", "version": "test"}
        self.next_cursor_override = None
        self.envelope_mode = None
        self.tool_rpc_error = False
        self.protocol_headers = []
        self.delete_calls = 0
        self.delete_headers = []
        self.delete_status = 204
        self.delete_unauthorized_once = False
        self.session_404_once = False
        self.always_404_methods = set()
        self.blocked_method = None
        self.blocked_method_entered = asyncio.Event()
        self.blocked_method_release = asyncio.Event()
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
            if method == self.blocked_method:
                self.blocked_method_entered.set()
                await self.blocked_method_release.wait()
            if method in self.always_404_methods:
                return web.json_response({"error": "session expired"}, status=404)
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
                            "capabilities": self.initialize_capabilities,
                            "serverInfo": self.initialize_server_info,
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

        async def delete_handler(request: web.Request) -> web.Response:
            self.delete_calls += 1
            self.delete_headers.append(
                (
                    request.headers.get("Mcp-Session-Id"),
                    request.headers.get("MCP-Protocol-Version"),
                )
            )
            if (
                self.delete_unauthorized_once
                and request.headers.get("Authorization") == "Bearer old-access"
            ):
                self.delete_unauthorized_once = False
                return web.Response(status=401)
            return web.Response(status=self.delete_status)

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

    async def test_missing_tools_capability_fails_before_initialized_notification(self):
        self.initialize_capabilities = {}
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "tools capability"):
                await client.connect()
            self.assertEqual([item["method"] for item in self.requests], ["initialize"])
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        finally:
            await client.close()

    async def test_non_object_tools_capability_fails_closed(self):
        self.initialize_capabilities = {"tools": True}
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "tools capability"):
                await client.connect()
            self.assertEqual([item["method"] for item in self.requests], ["initialize"])
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        finally:
            await client.close()

    async def test_non_boolean_list_changed_capability_fails_closed(self):
        self.initialize_capabilities = {"tools": {"listChanged": "yes"}}
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "tools capability"):
                await client.connect()
            self.assertEqual([item["method"] for item in self.requests], ["initialize"])
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        finally:
            await client.close()

    async def test_invalid_server_info_fails_closed(self):
        self.initialize_server_info = {"name": "Linear MCP", "version": 1}
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "server info"):
                await client.connect()
            self.assertEqual([item["method"] for item in self.requests], ["initialize"])
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        finally:
            await client.close()

    async def test_initialize_accepts_optional_and_additional_vendor_metadata(self):
        self.initialize_capabilities = {
            "tools": {"listChanged": True, "vendorExtension": {"enabled": True}},
            "logging": {},
        }
        self.initialize_server_info = {
            "name": "",
            "version": "",
            "title": "Linear",
            "websiteUrl": "https://linear.app",
        }
        client = self.client()
        try:
            await client.connect()
            self.assertEqual(client.session_id, "session-1")
            self.assertEqual(client.protocol_version, "2025-03-26")
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

    async def test_schema_drift_does_not_commit_an_executable_contract(self):
        target = next(tool for tool in self.tools if tool["name"] == "save_issue")
        target["inputSchema"]["properties"]["title"] = {"type": "integer"}
        client = self.client()
        try:
            with self.assertRaisesRegex(LinearMCPError, "schema drift"):
                await client.connect()
            self.assertEqual(client.tool_schemas, {})
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
            requests_before_call = len(self.requests)
            with self.assertRaisesRegex(LinearMCPError, "negotiated contract"):
                await client.call_tool("save_issue", {"title": "must-not-dispatch"})
            self.assertEqual(len(self.requests), requests_before_call)
        finally:
            await client.close()

    async def test_handshake_state_is_provisional_until_discovery_succeeds(self):
        self.protocol_version = "2025-06-18"
        self.blocked_method = "notifications/initialized"
        client = self.client()
        task = asyncio.create_task(client.connect())
        try:
            await self.blocked_method_entered.wait()
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
            self.assertEqual(client.tool_schemas, {})
            initialized_index = next(
                index
                for index, request in enumerate(self.requests)
                if request.get("method") == "notifications/initialized"
            )
            self.assertEqual(self.protocol_headers[initialized_index], "2025-06-18")
            task.cancel()
            self.blocked_method_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.delete_headers, [("session-1", "2025-06-18")])
            self.assertIsNone(client._session)
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
            self.assertEqual(client.tool_schemas, {})
        finally:
            if not task.done():
                task.cancel()
            self.blocked_method_release.set()
            try:
                await task
            except BaseException:
                pass
            await client.close()

    async def test_failed_repeat_discovery_invalidates_the_previous_contract(self):
        client = self.client()
        try:
            await client.connect()
            self.assertIn("save_issue", client.tool_schemas)
            target = next(tool for tool in self.tools if tool["name"] == "save_issue")
            target["inputSchema"]["properties"]["title"] = {"type": "integer"}
            with self.assertRaisesRegex(LinearMCPError, "schema drift"):
                await client.connect()
            self.assertEqual(client.tool_schemas, {})
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
            requests_before_call = len(self.requests)
            with self.assertRaisesRegex(LinearMCPError, "negotiated contract"):
                await client.call_tool("save_issue", {"title": "must-not-dispatch"})
            self.assertEqual(len(self.requests), requests_before_call)
        finally:
            await client.close()

    async def test_failed_handshake_deletes_provisional_session_with_negotiated_headers(self):
        self.protocol_version = "2025-06-18"
        target = next(tool for tool in self.tools if tool["name"] == "save_issue")
        target["inputSchema"]["properties"]["title"] = {"type": "integer"}
        client = self.client()
        with self.assertRaisesRegex(LinearMCPError, "schema drift"):
            await client.connect()
        self.assertEqual(self.delete_headers, [("session-1", "2025-06-18")])
        self.assertIsNone(client._session)
        self.assertIsNone(client.session_id)
        self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        self.assertEqual(client.tool_schemas, {})

    async def test_call_and_repeat_connect_are_serialized(self):
        client = self.client()
        try:
            await client.connect()
            original_send_rpc = client._send_rpc
            entered = asyncio.Event()
            release = asyncio.Event()

            async def blocking_send_rpc(method, params, *, mutation=False):
                if method == "tools/call":
                    entered.set()
                    await release.wait()
                return await original_send_rpc(method, params, mutation=mutation)

            client._send_rpc = blocking_send_rpc
            call_task = asyncio.create_task(client.call_tool("get_user", {"query": "me"}))
            await entered.wait()
            reconnect_task = asyncio.create_task(client.connect())
            await asyncio.sleep(0)
            self.assertFalse(reconnect_task.done())
            release.set()
            await call_task
            await reconnect_task
            self.assertTrue(client.tool_schemas)
            self.assertEqual(self.delete_calls, 1)
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
            for attribute in ("tool_rpc_error", "tool_error"):
                with self.subTest(attribute=attribute):
                    await client.connect()
                    setattr(self, attribute, True)
                    with self.assertRaises(MCPOutcomeUnknown):
                        await client.call_tool("save_issue", {"id": "OPS-1"}, mutation=True)
                    self.assertIsNone(client.session_id)
                    self.assertEqual(client.tool_schemas, {})
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

    async def test_close_invalidates_negotiated_protocol_and_contract(self):
        self.protocol_version = "2025-06-18"
        client = self.client()
        await client.connect()
        self.assertTrue(client.tool_schemas)
        await client.close()
        self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        self.assertIsNone(client.session_id)
        self.assertEqual(client.tool_schemas, {})

    async def test_close_token_failure_is_best_effort_and_never_masks(self):
        client = self.client()
        await client.connect()

        async def fail_token(*, force_refresh=False, stale_token=None):
            raise RuntimeError("token unavailable during close")

        self.store.access_token = fail_token
        await client.close()
        self.assertIsNone(client._session)
        self.assertIsNone(client.session_id)
        self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        self.assertEqual(client.tool_schemas, {})

    async def test_close_refreshes_delete_401_once_with_pinned_session_headers(self):
        self.protocol_version = "2025-06-18"
        client = self.client()
        await client.connect()
        self.delete_unauthorized_once = True
        await client.close()
        self.assertEqual(self.delete_calls, 2)
        self.assertEqual(
            self.delete_headers,
            [("session-1", "2025-06-18"), ("session-1", "2025-06-18")],
        )
        self.assertIn((True, "old-access"), self.store.calls)
        self.assertIsNone(client._session)
        self.assertIsNone(client.session_id)

    async def test_cancelled_close_still_invalidates_all_state(self):
        client = self.client()
        original_access_token = self.store.access_token
        await client.connect()
        entered = asyncio.Event()
        never = asyncio.Event()

        async def blocked_access_token(*, force_refresh=False, stale_token=None):
            entered.set()
            await never.wait()
            return await original_access_token(
                force_refresh=force_refresh,
                stale_token=stale_token,
            )

        self.store.access_token = blocked_access_token
        task = asyncio.create_task(client.close())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(client._session)
        self.assertIsNone(client.session_id)
        self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        self.assertEqual(client.tool_schemas, {})

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
            self.assertIsNone(client.session_id)
            self.assertEqual(client.tool_schemas, {})
        finally:
            await client.close()

    async def test_empty_mutation_session_header_is_outcome_unknown(self):
        client = self.client()
        try:
            await client.connect()
            self.tool_session_id_header = ""
            with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
            self.assertIsNone(client.session_id)
            self.assertEqual(client.tool_schemas, {})
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
            await client.connect()
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

    async def test_non_boolean_is_error_fails_read_closed(self):
        client = self.client()
        try:
            await client.connect()
            for value in ("true", 1, None, [], {}):
                with self.subTest(value=value):
                    self.tool_error = value
                    with self.assertRaisesRegex(LinearMCPError, "isError"):
                        await client.call_tool("get_user", {"query": "me"})
        finally:
            await client.close()

    async def test_non_boolean_mutation_is_error_is_outcome_unknown(self):
        client = self.client()
        try:
            for value in ("true", 1, None, [], {}):
                with self.subTest(value=value):
                    await client.connect()
                    self.tool_error = value
                    with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                        await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
                    self.assertIsNone(client.session_id)
                    self.assertEqual(client.tool_schemas, {})
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
            self.assertIsNone(client.session_id)
            self.assertEqual(client.tool_schemas, {})
        finally:
            await client.close()

    async def test_mutation_requires_authoritative_result_id(self):
        client = self.client()
        try:
            cases = [
                ("not-json", False),
                ('{"name":"missing-id"}', False),
                ('{"id":""}', False),
                ('{"id":"result-1"}', True),
            ]
            for text, accepted in cases:
                with self.subTest(text=text):
                    await client.connect()
                    self.tool_result_text = text
                    if accepted:
                        result = await client.call_tool("save_issue", {}, mutation=True)
                        self.assertEqual(result["content"][0]["text"], text)
                    else:
                        with self.assertRaises(MCPOutcomeUnknown):
                            await client.call_tool("save_issue", {}, mutation=True)
                        self.assertIsNone(client.session_id)
                        self.assertEqual(client.tool_schemas, {})

            await client.connect()
            self.empty_tool_content = True
            with self.assertRaises(MCPOutcomeUnknown):
                await client.call_tool("save_comment", {}, mutation=True)
            self.assertIsNone(client.session_id)
            self.assertEqual(client.tool_schemas, {})
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

    async def test_read_recovery_fails_closed_when_initialized_notification_loses_session(self):
        client = self.client()
        try:
            await client.connect()
            self.requests.clear()
            self.session_404_once = True
            self.always_404_methods = {"notifications/initialized"}
            with self.assertRaises(LinearMCPError):
                await asyncio.wait_for(
                    client.call_tool("get_user", {"query": "me"}),
                    timeout=1,
                )
            methods = [request.get("method") for request in self.requests]
            self.assertEqual(methods.count("tools/call"), 1)
            self.assertEqual(methods.count("initialize"), 1)
            self.assertEqual(methods.count("notifications/initialized"), 1)
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
            self.assertEqual(client.tool_schemas, {})
        finally:
            await client.close()

    async def test_read_recovery_fails_closed_when_tools_list_loses_session(self):
        client = self.client()
        try:
            await client.connect()
            self.requests.clear()
            self.session_404_once = True
            self.always_404_methods = {"tools/list"}
            with self.assertRaises(LinearMCPError):
                await asyncio.wait_for(
                    client.call_tool("get_user", {"query": "me"}),
                    timeout=1,
                )
            methods = [request.get("method") for request in self.requests]
            self.assertEqual(methods.count("tools/call"), 1)
            self.assertEqual(methods.count("initialize"), 1)
            self.assertEqual(methods.count("tools/list"), 1)
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
            self.assertEqual(client.tool_schemas, {})
        finally:
            await client.close()

    async def test_read_gets_only_one_recovery_when_redispatch_loses_session(self):
        client = self.client()
        try:
            await client.connect()
            self.requests.clear()
            self.always_404_methods = {"tools/call"}
            with self.assertRaisesRegex(LinearMCPError, "HTTP 404"):
                await client.call_tool("get_user", {"query": "me"})
            methods = [request.get("method") for request in self.requests]
            self.assertEqual(methods.count("tools/call"), 2)
            self.assertEqual(methods.count("initialize"), 1)
        finally:
            await client.close()

    async def test_cancelled_dispatched_mutation_has_unknown_outcome(self):
        client = self.client()
        try:
            await client.connect()
            self.delete_status = 500
            self.blocked_method = "tools/call"
            task = asyncio.create_task(
                client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
            )
            await self.blocked_method_entered.wait()
            task.cancel()
            with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
                await task
            tool_calls = [
                request for request in self.requests if request.get("method") == "tools/call"
            ]
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(self.delete_calls, 1)
            self.assertIsNone(client._session)
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
            self.assertEqual(client.tool_schemas, {})
        finally:
            self.blocked_method_release.set()
            await client.close()

    async def test_cancelled_mutation_preserves_unknown_when_cleanup_token_fails(self):
        client = self.client()
        await client.connect()
        original_access_token = self.store.access_token
        access_calls = 0

        async def fail_cleanup_access_token(*, force_refresh=False, stale_token=None):
            nonlocal access_calls
            access_calls += 1
            if access_calls == 1:
                return await original_access_token(
                    force_refresh=force_refresh,
                    stale_token=stale_token,
                )
            raise RuntimeError("cleanup token unavailable")

        self.store.access_token = fail_cleanup_access_token
        self.blocked_method = "tools/call"
        task = asyncio.create_task(
            client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
        )
        await self.blocked_method_entered.wait()
        task.cancel()
        with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
            await task
        self.blocked_method_release.set()
        self.assertEqual(access_calls, 2)
        self.assertIsNone(client._session)
        self.assertIsNone(client.session_id)
        self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        self.assertEqual(client.tool_schemas, {})

    async def test_mutation_refresh_failure_after_401_is_outcome_unknown(self):
        client = self.client()
        await client.connect()
        original_access_token = self.store.access_token

        async def fail_refresh(*, force_refresh=False, stale_token=None):
            if force_refresh:
                raise RuntimeError("refresh unavailable")
            return await original_access_token(
                force_refresh=force_refresh,
                stale_token=stale_token,
            )

        self.store.access_token = fail_refresh
        self.requests.clear()
        self.unauthorized_once = True
        with self.assertRaisesRegex(MCPOutcomeUnknown, "outcome is unknown"):
            await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
        tool_calls = [item for item in self.requests if item.get("method") == "tools/call"]
        self.assertEqual(len(tool_calls), 1)
        self.assertIsNone(client._session)
        self.assertIsNone(client.session_id)
        self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        self.assertEqual(client.tool_schemas, {})

    async def test_lost_session_reinitialize_header_matches_initialize_body(self):
        self.protocol_version = "2025-06-18"
        client = self.client()
        try:
            await client.connect()
            self.requests.clear()
            self.protocol_headers.clear()
            self.session_404_once = True
            await client.call_tool("get_user", {"query": "me"})
            paired = list(zip(self.requests, self.protocol_headers, strict=True))
            reinitialize = [
                (request, header)
                for request, header in paired
                if request.get("method") == "initialize"
            ]
            self.assertEqual(len(reinitialize), 1)
            request, header = reinitialize[0]
            self.assertEqual(request["params"]["protocolVersion"], INITIAL_PROTOCOL_VERSION)
            self.assertEqual(header, INITIAL_PROTOCOL_VERSION)
            self.assertEqual(client.protocol_version, "2025-06-18")
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
            self.assertIsNone(client.session_id)
            self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
            self.assertEqual(client.tool_schemas, {})
        finally:
            await client.close()

    async def test_mutation_lost_session_preserves_unknown_when_cleanup_token_fails(self):
        client = self.client()
        await client.connect()
        original_access_token = self.store.access_token
        access_calls = 0

        async def fail_cleanup_access_token(*, force_refresh=False, stale_token=None):
            nonlocal access_calls
            access_calls += 1
            if access_calls == 1:
                return await original_access_token(
                    force_refresh=force_refresh,
                    stale_token=stale_token,
                )
            raise RuntimeError("cleanup token unavailable")

        self.store.access_token = fail_cleanup_access_token
        self.requests.clear()
        self.session_404_once = True
        with self.assertRaises(MCPOutcomeUnknown):
            await client.call_tool("save_issue", {"title": "safe-test"}, mutation=True)
        self.assertEqual(access_calls, 2)
        self.assertIsNone(client._session)
        self.assertIsNone(client.session_id)
        self.assertEqual(client.protocol_version, INITIAL_PROTOCOL_VERSION)
        self.assertEqual(client.tool_schemas, {})

if __name__ == "__main__":
    unittest.main()
