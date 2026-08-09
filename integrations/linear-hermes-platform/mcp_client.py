"""Narrow Streamable HTTP client for Linear's official MCP server."""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Protocol

import aiohttp

try:
    from .oauth_store import LinearOAuthStore
except ImportError:  # Direct module loading in standalone tests/scripts.
    from oauth_store import LinearOAuthStore

OFFICIAL_LINEAR_MCP_ENDPOINT = "https://mcp.linear.app/mcp"
INITIAL_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-03-26", "2025-06-18"})
OFFICIAL_LINEAR_INPUT_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
WORKSPACE_TOOL_INPUT_SCHEMA = {"type": "object", "properties": {}}
REQUIRED_TOOLS = frozenset({"get_user", "get_issue", "list_issues", "save_issue", "save_comment"})
EXECUTABLE_VENDOR_TOOLS = REQUIRED_TOOLS
MUTATION_VENDOR_TOOLS = frozenset({"save_issue", "save_comment"})
EXPECTED_VENDOR_TOOL_NAMES = frozenset(
    {
        "create_attachment", "create_attachment_from_upload", "create_issue_label",
        "delete_attachment", "delete_comment", "delete_diff_comment",
        "delete_status_update", "extract_images", "get_agent_skill", "get_attachment",
        "get_diff", "get_diff_threads", "get_document", "get_issue", "get_issue_status",
        "get_milestone", "get_project", "get_release", "get_release_note",
        "get_status_updates", "get_team", "get_user", "get_workspace", "list_agent_skills",
        "list_comments", "list_cycles", "list_diffs", "list_documents",
        "list_issue_labels", "list_issue_statuses", "list_issues", "list_milestones",
        "list_project_labels", "list_projects", "list_release_notes",
        "list_release_pipelines", "list_releases", "list_teams", "list_users",
        "merge_diff", "prepare_attachment_upload", "resolve_diff_thread", "save_comment",
        "save_diff_comment", "save_document", "save_issue", "save_milestone",
        "save_project", "save_release", "save_release_note", "save_status_update",
        "search_documentation", "submit_diff_review",
    }
)
REQUIRED_TOOL_INPUT_FIELDS = {
    "get_user": frozenset({"query"}),
    "get_issue": frozenset({"id", "includeRelations"}),
    "list_issues": frozenset(
        {
            "team", "limit", "cursor", "orderBy", "query", "state", "assignee",
            "delegate", "project", "cycle", "label", "createdAt", "updatedAt",
            "includeArchived",
        }
    ),
    "save_issue": frozenset(
        {
            "id", "title", "description", "team", "state", "assignee", "delegate",
            "project", "milestone", "cycle", "labels", "parentId", "priority",
            "estimate", "dueDate", "blocks", "blockedBy", "relatedTo", "removeBlocks",
            "removeBlockedBy", "removeRelatedTo",
        }
    ),
    "save_comment": frozenset({"id", "issueId", "body"}),
}
LIVE_TOOL_PROPERTY_FIELDS = {
    "get_user": frozenset({"query"}),
    "get_issue": frozenset(
        {"id", "includeRelations", "includeCustomerNeeds", "includeReleases"}
    ),
    "list_issues": REQUIRED_TOOL_INPUT_FIELDS["list_issues"]
    | frozenset({"fields", "parentId", "priority", "release"}),
    "save_issue": REQUIRED_TOOL_INPUT_FIELDS["save_issue"]
    | frozenset(
        {
            "addReleases", "duplicateOf", "links", "patch", "removeReleases",
            "setReleases", "slaBreachesAt", "slaType",
        }
    ),
    "save_comment": REQUIRED_TOOL_INPUT_FIELDS["save_comment"]
    | frozenset(
        {
            "documentId", "initiativeId", "milestoneId", "parentId", "projectId",
            "statusUpdateId", "statusUpdateType",
        }
    ),
}
ARRAY_INPUT_FIELDS = frozenset(
    {
        "labels", "blocks", "blockedBy", "relatedTo",
        "removeBlocks", "removeBlockedBy", "removeRelatedTo",
    }
)
INTEGER_INPUT_FIELDS = frozenset()
NUMBER_INPUT_FIELDS = frozenset({"estimate", "limit", "priority"})
BOOLEAN_INPUT_FIELDS = frozenset({"includeRelations", "includeArchived"})
REQUIRED_VENDOR_INPUT_FIELDS = {
    "get_user": frozenset({"query"}),
    "get_issue": frozenset({"id"}),
    "list_issues": frozenset(),
    "save_issue": frozenset(),
    "save_comment": frozenset({"body"}),
}
MAX_TOOL_LIST_PAGES = 20
MAX_TOOLS_PER_PAGE = 100
MAX_TOTAL_TOOLS = 256
MAX_CURSOR_LENGTH = 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SESSION_ID_BYTES = 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000
MAX_SSE_EVENTS = 16
MAX_CONTENT_ITEMS = 16
MAX_TEXT_LENGTH = 256 * 1024


def _json_schema_types(schema: Any) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    found: set[str] = set()
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        found.add(raw_type)
    elif isinstance(raw_type, list):
        found.update(value for value in raw_type if isinstance(value, str))
    for keyword in ("anyOf", "oneOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for variant in variants:
                found.update(_json_schema_types(variant))
    return found


def _expected_input_type(field: str) -> str:
    if field in ARRAY_INPUT_FIELDS:
        return "array"
    if field in INTEGER_INPUT_FIELDS:
        return "integer"
    if field in NUMBER_INPUT_FIELDS:
        return "number"
    if field in BOOLEAN_INPUT_FIELDS:
        return "boolean"
    return "string"


NULLABLE_VENDOR_FIELDS = frozenset(
    {
        ("list_issues", "assignee"),
        ("save_issue", "assignee"),
        ("save_issue", "cycle"),
        ("save_issue", "delegate"),
        ("save_issue", "dueDate"),
        ("save_issue", "estimate"),
        ("save_issue", "parentId"),
        ("save_issue", "project"),
    }
)


def _expected_forwarded_contract(tool_name: str, field: str) -> dict[str, Any]:
    expected_type = _expected_input_type(field)
    if (tool_name, field) in NULLABLE_VENDOR_FIELDS:
        contract: dict[str, Any] = {
            "anyOf": [{"type": expected_type}, {"type": "null"}]
        }
    else:
        contract = {"type": expected_type}
    if expected_type == "array":
        contract["items"] = {"type": "string"}
    if (tool_name, field) == ("get_issue", "includeRelations"):
        contract["default"] = False
    elif (tool_name, field) == ("list_issues", "includeArchived"):
        contract["default"] = True
    elif (tool_name, field) == ("list_issues", "limit"):
        contract.update({"default": 50, "maximum": 250})
    elif (tool_name, field) == ("list_issues", "orderBy"):
        contract.update({"default": "updatedAt", "enum": ["createdAt", "updatedAt"]})
    return contract


def _schema_without_descriptions(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {
            key: _schema_without_descriptions(value)
            for key, value in schema.items()
            if key != "description"
        }
    if isinstance(schema, list):
        return [_schema_without_descriptions(value) for value in schema]
    return schema


def _validate_json_limits(value: Any) -> None:
    stack = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON structure exceeded limits")
        if isinstance(current, dict):
            for key, child in current.items():
                if not isinstance(key, str) or len(key) > MAX_TEXT_LENGTH:
                    raise ValueError("JSON key exceeded limits")
                stack.append((child, depth + 1))
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
        elif isinstance(current, str) and len(current) > MAX_TEXT_LENGTH:
            raise ValueError("JSON string exceeded limits")


class OAuthTokenProvider(Protocol):
    async def access_token(
        self,
        *,
        force_refresh: bool = False,
        stale_token: str | None = None,
    ) -> str: ...


class LinearMCPError(RuntimeError):
    pass


class LinearMCPToolError(LinearMCPError):
    """The MCP server explicitly rejected a tool call without committing it."""


class MCPOutcomeUnknown(LinearMCPError):
    """A mutation may have committed but no authoritative response arrived."""


class _MCPSessionLost(LinearMCPError):
    """Internal signal used to recover a read after its response context closes."""


class _MCPMutationSessionLost(MCPOutcomeUnknown):
    """Internal signal used to clean up without redispatching a mutation."""


class LinearMCPClient:
    def __init__(
        self,
        oauth_store: LinearOAuthStore | OAuthTokenProvider,
        *,
        endpoint: str = OFFICIAL_LINEAR_MCP_ENDPOINT,
        timeout_seconds: float = 12.0,
        required_tools: frozenset[str] = REQUIRED_TOOLS,
        allow_test_endpoint: bool = False,
    ) -> None:
        if not allow_test_endpoint and endpoint != OFFICIAL_LINEAR_MCP_ENDPOINT:
            raise LinearMCPError("Linear MCP bearer transport requires the exact official endpoint")
        self.oauth_store = oauth_store
        self.endpoint = endpoint
        self.timeout_seconds = float(timeout_seconds)
        self.required_tools = frozenset(required_tools)
        self.session_id: str | None = None
        self.protocol_version = INITIAL_PROTOCOL_VERSION
        self.tool_schemas: dict[str, dict[str, Any]] = {}
        self._session: aiohttp.ClientSession | None = None
        self._provisional_session_id: str | None = None
        self._provisional_protocol_version: str | None = None
        self._ids = itertools.count(1)
        self._state_lock = asyncio.Lock()

    def _invalidate_negotiated_state(self) -> None:
        self.session_id = None
        self.protocol_version = INITIAL_PROTOCOL_VERSION
        self.tool_schemas = {}
        self._provisional_session_id = None
        self._provisional_protocol_version = None

    async def connect(self) -> None:
        async with self._state_lock:
            if self._session is not None:
                await self._close_unlocked()
            await self._connect_unlocked()

    async def _connect_unlocked(self) -> None:
        self._invalidate_negotiated_state()
        try:
            await self._connect_impl()
        except BaseException:
            try:
                await self._close_unlocked()
            except BaseException:
                self._invalidate_negotiated_state()
            raise

    async def _connect_impl(self) -> None:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds, connect=4)
            self._session = aiohttp.ClientSession(timeout=timeout)
        initialize, response_session_id = await self._send_rpc(
            "initialize",
            {
                "protocolVersion": INITIAL_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hermes-linear", "version": "0.6.0"},
            },
        )
        negotiated = str(initialize.get("protocolVersion") or "")
        if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
            raise LinearMCPError("Linear MCP negotiated an unsupported protocol version")
        self._provisional_protocol_version = negotiated
        capabilities = initialize.get("capabilities")
        tools_capability = capabilities.get("tools") if isinstance(capabilities, dict) else None
        if not isinstance(tools_capability, dict):
            raise LinearMCPError("Linear MCP initialize omitted a valid tools capability")
        if "listChanged" in tools_capability and not isinstance(
            tools_capability["listChanged"], bool
        ):
            raise LinearMCPError("Linear MCP initialize returned an invalid tools capability")
        server_info = initialize.get("serverInfo")
        if not isinstance(server_info, dict) or any(
            not isinstance(server_info.get(field), str)
            for field in ("name", "version")
        ):
            raise LinearMCPError("Linear MCP initialize returned invalid server info")
        self._provisional_session_id = response_session_id
        await self._send_notification("notifications/initialized", {})
        tools: list[dict[str, Any]] = []
        tool_names: set[str] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_TOOL_LIST_PAGES):
            listed, _response_session_id = await self._send_rpc(
                "tools/list", {"cursor": cursor} if cursor else {}
            )
            page_tools = listed.get("tools")
            if not isinstance(page_tools, list) or len(page_tools) > MAX_TOOLS_PER_PAGE:
                raise LinearMCPError("Linear MCP tools/list returned an invalid contract")
            for tool in page_tools:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    raise LinearMCPError("Linear MCP tools/list returned an invalid tool")
                tool_name = tool["name"]
                if not tool_name or len(tool_name) > 128 or tool_name in tool_names:
                    raise LinearMCPError("Linear MCP tools/list returned a duplicate or invalid tool")
                tool_names.add(tool_name)
                tools.append(tool)
                if len(tools) > MAX_TOTAL_TOOLS:
                    raise LinearMCPError("Linear MCP tools/list exceeded the total tool limit")
            next_cursor = listed.get("nextCursor")
            if next_cursor in (None, ""):
                break
            if not isinstance(next_cursor, str) or len(next_cursor) > MAX_CURSOR_LENGTH:
                raise LinearMCPError("Linear MCP tools/list returned an invalid cursor")
            cursor = next_cursor
            if cursor in seen_cursors:
                raise LinearMCPError("Linear MCP tools/list cursor repeated")
            seen_cursors.add(cursor)
        else:
            raise LinearMCPError("Linear MCP tools/list exceeded the page limit")
        if tool_names != EXPECTED_VENDOR_TOOL_NAMES:
            raise LinearMCPError("Linear MCP vendor tool-name contract drifted")
        validated_tool_schemas = {tool["name"]: tool for tool in tools}
        missing = sorted(self.required_tools - set(validated_tool_schemas))
        if missing:
            raise LinearMCPError(f"Linear MCP missing required tools: {', '.join(missing)}")
        for tool_name, tool in validated_tool_schemas.items():
            schema = tool.get("inputSchema")
            if not isinstance(schema, dict):
                raise LinearMCPError(f"Linear MCP tool schema drift: {tool_name}")
            if tool_name == "get_workspace":
                if schema != WORKSPACE_TOOL_INPUT_SCHEMA:
                    raise LinearMCPError(f"Linear MCP tool schema drift: {tool_name}")
                continue
            if schema.get("$schema") != OFFICIAL_LINEAR_INPUT_SCHEMA_URI:
                raise LinearMCPError(f"Linear MCP tool schema drift: {tool_name}")
        for tool_name, required_fields in REQUIRED_TOOL_INPUT_FIELDS.items():
            schema = validated_tool_schemas[tool_name].get("inputSchema")
            properties = schema.get("properties") if isinstance(schema, dict) else None
            pinned_required = REQUIRED_VENDOR_INPUT_FIELDS[tool_name]
            expected_root_keys = {
                "$schema",
                "type",
                "properties",
                "additionalProperties",
            }
            if pinned_required:
                expected_root_keys.add("required")
            if (
                not isinstance(schema, dict)
                or set(schema) != expected_root_keys
                or schema.get("$schema") != OFFICIAL_LINEAR_INPUT_SCHEMA_URI
                or schema.get("type") != "object"
                or schema.get("additionalProperties") is not False
                or not isinstance(properties, dict)
                or set(properties) != LIVE_TOOL_PROPERTY_FIELDS[tool_name]
            ):
                raise LinearMCPError(f"Linear MCP tool schema drift: {tool_name}")
            for field in required_fields:
                property_schema = properties[field]
                if _schema_without_descriptions(property_schema) != _expected_forwarded_contract(
                    tool_name, field
                ):
                    raise LinearMCPError(f"Linear MCP tool schema drift: {tool_name}.{field}")
            if pinned_required:
                required_by_vendor = schema.get("required")
                if (
                    not isinstance(required_by_vendor, list)
                    or any(not isinstance(value, str) for value in required_by_vendor)
                    or set(required_by_vendor) != pinned_required
                    or len(required_by_vendor) != len(pinned_required)
                ):
                    raise LinearMCPError(f"Linear MCP tool schema drift: {tool_name}.required")
        self.session_id = self._provisional_session_id
        self.protocol_version = negotiated
        self.tool_schemas = validated_tool_schemas
        self._provisional_session_id = None
        self._provisional_protocol_version = None

    async def close(self) -> None:
        async with self._state_lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        session = self._session
        if session is None:
            self._invalidate_negotiated_state()
            return
        session_id = self._provisional_session_id or self.session_id
        protocol_version = (
            self._provisional_protocol_version or self.protocol_version
        )

        async def delete_once(token: str) -> int:
            async with session.delete(
                self.endpoint,
                headers=self._headers_for_state(
                    token,
                    protocol_version=protocol_version,
                    session_id=session_id,
                ),
                allow_redirects=False,
            ) as response:
                return response.status

        try:
            if session_id:
                try:
                    token = await self.oauth_store.access_token()
                    status = await delete_once(token)
                    if status == 401:
                        refreshed = await self.oauth_store.access_token(
                            force_refresh=True,
                            stale_token=token,
                        )
                        await delete_once(refreshed)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        finally:
            self._session = None
            self._invalidate_negotiated_state()
            await session.close()

    def _headers(self, token: str) -> dict[str, str]:
        return self._headers_for_state(
            token,
            protocol_version=(
                self._provisional_protocol_version or self.protocol_version
            ),
            session_id=self._provisional_session_id or self.session_id,
        )

    @staticmethod
    def _headers_for_state(
        token: str,
        *,
        protocol_version: str,
        session_id: str | None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": protocol_version,
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    @staticmethod
    def _validate_session_id(value: str) -> str:
        if (
            not value
            or len(value.encode("ascii", errors="ignore")) > MAX_SESSION_ID_BYTES
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
        ):
            raise ValueError("invalid MCP session id")
        return value

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        if self._session is None:
            raise LinearMCPError("Linear MCP client is not connected")
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        token = await self.oauth_store.access_token()
        try:
            async with self._session.post(
                self.endpoint,
                headers=self._headers(token),
                json=payload,
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    token = await self.oauth_store.access_token(
                        force_refresh=True,
                        stale_token=token,
                    )
                    async with self._session.post(
                        self.endpoint,
                        headers=self._headers(token),
                        json=payload,
                        allow_redirects=False,
                    ) as retried:
                        if retried.status < 200 or retried.status >= 300:
                            raise LinearMCPError("Linear MCP notification was rejected")
                    return
                if response.status < 200 or response.status >= 300:
                    raise LinearMCPError("Linear MCP notification was rejected")
        except asyncio.TimeoutError as exc:
            raise LinearMCPError("Linear MCP notification timed out") from exc

    async def _send_rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        mutation: bool = False,
        allow_session_recovery: bool = True,
    ) -> tuple[dict[str, Any], str | None]:
        request_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        envelope, response_session_id = await self._post(
            payload,
            request_id=request_id,
            mutation=mutation,
            accept_session_id=method == "initialize",
            allow_session_recovery=allow_session_recovery,
        )
        has_result = "result" in envelope
        has_error = "error" in envelope
        if (
            envelope.get("jsonrpc") != "2.0"
            or type(envelope.get("id")) is not int
            or envelope.get("id") != request_id
            or has_result == has_error
        ):
            if mutation:
                raise MCPOutcomeUnknown("Linear mutation JSON-RPC envelope was invalid; outcome is unknown")
            raise LinearMCPError("Linear MCP returned an invalid JSON-RPC envelope")
        if has_error:
            if mutation:
                raise MCPOutcomeUnknown("Linear mutation returned a JSON-RPC error; outcome is unknown")
            raise LinearMCPError("Linear MCP returned a JSON-RPC error")
        result = envelope["result"]
        if not isinstance(result, dict):
            if mutation:
                raise MCPOutcomeUnknown("Linear mutation result was invalid; outcome is unknown")
            raise LinearMCPError("Linear MCP result was not an object")
        return result, response_session_id

    @staticmethod
    async def _read_bounded_response(response: aiohttp.ClientResponse) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeded byte limit")
        body = bytearray()
        async for chunk in response.content.iter_chunked(65_536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeded byte limit")
        return bytes(body)

    @staticmethod
    async def _decode_response(
        response: aiohttp.ClientResponse,
        *,
        request_id: int,
        mutation: bool,
    ) -> dict[str, Any]:
        content_types = response.headers.getall("Content-Type", [])
        media_type = ""
        valid_content_type = len(content_types) == 1
        if valid_content_type:
            parts = [part.strip() for part in content_types[0].split(";")]
            media_type = parts[0].lower()
            valid_content_type = media_type in {"application/json", "text/event-stream"}
            if len(parts) == 2:
                key, separator, value = parts[1].partition("=")
                raw_charset = value.strip()
                valid_charset = raw_charset.lower() == "utf-8" or (
                    len(raw_charset) >= 2
                    and raw_charset[0] == raw_charset[-1] == '"'
                    and raw_charset[1:-1].lower() == "utf-8"
                )
                valid_content_type = bool(
                    valid_content_type
                    and separator
                    and key.strip().lower() == "charset"
                    and valid_charset
                )
            elif len(parts) != 1:
                valid_content_type = False
        if not valid_content_type:
            if mutation:
                raise MCPOutcomeUnknown(
                    "Linear mutation response had an invalid content type; outcome is unknown"
                )
            raise LinearMCPError("Linear MCP response had an invalid content type")
        try:
            raw = await LinearMCPClient._read_bounded_response(response)
            text = raw.decode("utf-8")
            if media_type == "text/event-stream":
                matches: list[dict[str, Any]] = []
                data_lines: list[str] = []
                event_count = 0
                for line in text.splitlines() + [""]:
                    if line == "":
                        if data_lines:
                            event_count += 1
                            if event_count > MAX_SSE_EVENTS:
                                raise ValueError("too many SSE events")
                            parsed = json.loads("\n".join(data_lines))
                            if not isinstance(parsed, dict):
                                raise ValueError("non-object SSE response")
                            _validate_json_limits(parsed)
                            response_id = parsed.get("id")
                            if type(response_id) is int and response_id == request_id:
                                matches.append(parsed)
                        data_lines = []
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                if len(matches) != 1:
                    raise ValueError("ambiguous or missing matching SSE response")
                return matches[0]
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("non-object response")
            _validate_json_limits(parsed)
            return parsed
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            aiohttp.ContentTypeError,
        ) as exc:
            if mutation:
                raise MCPOutcomeUnknown("Linear mutation response was invalid; outcome is unknown") from exc
            raise LinearMCPError("Linear MCP response was not valid bounded JSON-RPC") from exc

    async def _post(
        self,
        payload: dict[str, Any],
        *,
        request_id: int,
        mutation: bool,
        accept_session_id: bool,
        allow_refresh: bool = True,
        allow_read_retry: bool = True,
        allow_session_recovery: bool = True,
    ) -> tuple[dict[str, Any], str | None]:
        if self._session is None:
            raise LinearMCPError("Linear MCP client is not connected")
        refresh_available = allow_refresh
        read_retry_available = allow_read_retry
        dispatched = False
        try:
            while True:
                token = await self.oauth_store.access_token()
                retry_delay: float | None = None
                retry_after_refresh = False
                dispatched = True
                async with self._session.post(
                    self.endpoint,
                    headers=self._headers(token),
                    json=payload,
                    allow_redirects=False,
                ) as response:
                    if response.status == 401 and refresh_available:
                        try:
                            await self.oauth_store.access_token(
                                force_refresh=True,
                                stale_token=token,
                            )
                        except asyncio.CancelledError as exc:
                            if mutation:
                                raise MCPOutcomeUnknown(
                                    "Linear mutation authentication refresh was cancelled; outcome is unknown"
                                ) from exc
                            raise
                        except Exception as exc:
                            if mutation:
                                raise MCPOutcomeUnknown(
                                    "Linear mutation authentication refresh failed; outcome is unknown"
                                ) from exc
                            raise LinearMCPError(
                                "Linear MCP authentication refresh failed"
                            ) from exc
                        if mutation:
                            raise MCPOutcomeUnknown(
                                "Linear mutation authentication changed; mutation was not retried and outcome is unknown"
                            )
                        refresh_available = False
                        retry_after_refresh = True
                    elif (
                        response.status == 404
                        and (self._provisional_session_id or self.session_id)
                        and allow_session_recovery
                    ):
                        if mutation:
                            raise _MCPMutationSessionLost(
                                "Linear mutation session was lost; outcome is unknown"
                            )
                        raise _MCPSessionLost("Linear MCP HTTP 404: session was lost")
                    elif response.status == 429 or response.status >= 500:
                        if mutation:
                            raise MCPOutcomeUnknown(
                                f"Linear mutation HTTP {response.status}; outcome is unknown"
                            )
                        if read_retry_available:
                            retry_after = response.headers.get("Retry-After")
                            try:
                                retry_delay = min(2.0, max(0.0, float(retry_after or 0.2)))
                            except ValueError:
                                retry_delay = 0.2
                            read_retry_available = False
                        else:
                            raise LinearMCPError("Linear MCP read failed after retry")
                    elif response.status < 200 or response.status >= 300:
                        if mutation:
                            raise MCPOutcomeUnknown(
                                f"Linear mutation HTTP {response.status}; outcome is unknown"
                            )
                        raise LinearMCPError(f"Linear MCP HTTP {response.status}")
                    else:
                        response_session_headers = response.headers.getall(
                            "Mcp-Session-Id", []
                        )
                        response_session_id: str | None = None
                        if response_session_headers:
                            try:
                                if len(response_session_headers) != 1:
                                    raise ValueError("duplicate MCP session id")
                                validated_session_id = self._validate_session_id(
                                    response_session_headers[0]
                                )
                            except ValueError as exc:
                                if mutation:
                                    raise MCPOutcomeUnknown(
                                        "Linear mutation returned an invalid session id; outcome is unknown"
                                    ) from exc
                                raise LinearMCPError(
                                    "Linear MCP returned an invalid session id"
                                ) from exc
                            if not accept_session_id:
                                if mutation:
                                    raise MCPOutcomeUnknown(
                                        "Linear mutation returned an unexpected session id; outcome is unknown"
                                    )
                                raise LinearMCPError(
                                    "Linear MCP returned an unexpected session id"
                                )
                            response_session_id = validated_session_id
                            self._provisional_session_id = validated_session_id
                        envelope = await self._decode_response(
                            response,
                            request_id=request_id,
                            mutation=mutation,
                        )
                        return envelope, response_session_id
                if retry_after_refresh:
                    continue
                if retry_delay is not None:
                    await asyncio.sleep(retry_delay)
                    continue
        except MCPOutcomeUnknown:
            raise
        except asyncio.CancelledError as exc:
            if mutation and dispatched:
                raise MCPOutcomeUnknown(
                    "Linear mutation was cancelled after dispatch; outcome is unknown"
                ) from exc
            raise
        except asyncio.TimeoutError as exc:
            if mutation:
                raise MCPOutcomeUnknown("Linear mutation timed out; outcome is unknown") from exc
            raise LinearMCPError("Linear MCP read timed out") from exc
        except aiohttp.ClientError as exc:
            if mutation:
                raise MCPOutcomeUnknown("Linear mutation transport failed; outcome is unknown") from exc
            raise LinearMCPError("Linear MCP transport failed") from exc

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        mutation: bool = False,
    ) -> dict[str, Any]:
        async with self._state_lock:
            return await self._call_tool_unlocked(name, arguments, mutation=mutation)

    async def _discard_ambiguous_session_unlocked(self) -> None:
        try:
            await self._close_unlocked()
        except BaseException:
            pass

    async def _call_tool_unlocked(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        mutation: bool = False,
    ) -> dict[str, Any]:
        if name not in EXECUTABLE_VENDOR_TOOLS:
            raise LinearMCPError(f"Linear MCP tool is not authorized for execution: {name}")
        if name not in self.tool_schemas:
            raise LinearMCPError(f"Linear MCP tool is not in the negotiated contract: {name}")
        derived_mutation = name in MUTATION_VENDOR_TOOLS
        if mutation and not derived_mutation:
            raise LinearMCPError(f"Linear MCP read tool cannot be classified as mutation: {name}")
        call_params = {"name": name, "arguments": arguments}
        try:
            result, _response_session_id = await self._send_rpc(
                "tools/call",
                call_params,
                mutation=derived_mutation,
            )
        except _MCPMutationSessionLost:
            await self._discard_ambiguous_session_unlocked()
            raise
        except _MCPSessionLost:
            await self._close_unlocked()
            await self._connect_unlocked()
            result, _response_session_id = await self._send_rpc(
                "tools/call",
                call_params,
                mutation=False,
                allow_session_recovery=False,
            )
        except MCPOutcomeUnknown:
            await self._discard_ambiguous_session_unlocked()
            raise
        is_error = result.get("isError", False)
        if "isError" in result and type(is_error) is not bool:
            if derived_mutation:
                try:
                    await self._close_unlocked()
                except BaseException:
                    pass
                raise MCPOutcomeUnknown(
                    f"Linear MCP tool {name} returned an invalid isError value; outcome is unknown"
                )
            raise LinearMCPError(f"Linear MCP tool {name} returned an invalid isError value")
        if is_error:
            if derived_mutation:
                try:
                    await self._close_unlocked()
                except BaseException:
                    pass
                raise MCPOutcomeUnknown(
                    f"Linear MCP tool {name} reported an error; outcome is unknown"
                )
            raise LinearMCPToolError(f"Linear MCP tool {name} reported an error")
        content = result.get("content")
        valid_content = (
            isinstance(content, list)
            and 0 < len(content) <= MAX_CONTENT_ITEMS
            and all(
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and len(item["text"]) <= MAX_TEXT_LENGTH
            for item in content
        )
        )
        if not valid_content:
            if derived_mutation:
                await self._discard_ambiguous_session_unlocked()
                raise MCPOutcomeUnknown(
                    f"Linear MCP tool {name} returned an invalid result contract; outcome is unknown"
                )
            raise LinearMCPError(f"Linear MCP tool {name} returned an invalid result contract")
        assert isinstance(content, list)
        if derived_mutation:
            try:
                parsed = json.loads(content[0]["text"]) if len(content) == 1 else None
            except (TypeError, ValueError):
                parsed = None
            if not isinstance(parsed, dict) or not isinstance(parsed.get("id"), str) or not parsed["id"]:
                await self._discard_ambiguous_session_unlocked()
                raise MCPOutcomeUnknown(
                    f"Linear MCP tool {name} returned no authoritative result id; outcome is unknown"
                )
        return result
