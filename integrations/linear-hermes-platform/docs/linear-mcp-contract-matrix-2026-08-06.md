# Linear official MCP contract matrix — 2026-08-06

- Negotiated Linear protocol: `2025-03-26`.
- Transport: the initialization-era Streamable HTTP + JSON-RPC contract. Linear's live initialize response advertises a `tools` capability with `listChanged: true`; `capabilities`, the tools capability, `listChanged`, and string-valued `serverInfo.name/version` are validated before protocol/session state is committed, while optional vendor metadata remains accepted under the global response limits. Linear's live initialize response did not issue a session ID; the client nevertheless supports the negotiated-era optional session contract with bounded validation.
- Current public MCP specification: `2026-07-28`. It removes protocol-level sessions and changes request metadata/transport semantics. Those behaviors are not enabled speculatively because Linear currently negotiates `2025-03-26`; an unsupported negotiation fails closed.
- Live inventory: **52 tools**. The exact name set and draft-2020-12 URI are enforced for all 52. Exact root keys, property sets and requiredness are enforced for the five locally required vendor tools; type/nullability/format checks cover every locally forwarded field in those contracts.
- Local model surface: `linear_get_issue`, `linear_list_issues`, `linear_save_issue`, `linear_save_comment`.
- Terminal project mutation: **not exposed**. `linear_complete_project` is rejected fail-closed.
- Sources: [negotiated-era MCP spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports), [current MCP spec](https://modelcontextprotocol.io/specification/2026-07-28), [Linear MCP](https://linear.app/docs/mcp), [Linear OAuth](https://linear.app/developers/oauth-2-0-authentication), [Linear webhooks](https://linear.app/developers/webhooks).

## Live 52-tool inventory

| # | Vendor tool | Required input | Local semantic use |
|---:|---|---|---|
| 1 | `create_attachment` | `issue, base64Content, filename, contentType, sha256` | Not model-exposed; drift validation only |
| 2 | `create_attachment_from_upload` | `issue, assetUrl` | Not model-exposed; drift validation only |
| 3 | `create_issue_label` | `name` | Not model-exposed; drift validation only |
| 4 | `delete_attachment` | `id` | Not model-exposed; drift validation only |
| 5 | `delete_comment` | `id` | Not model-exposed; drift validation only |
| 6 | `delete_diff_comment` | `commentId` | Not model-exposed; drift validation only |
| 7 | `delete_status_update` | `type, id` | Not model-exposed; drift validation only |
| 8 | `extract_images` | `markdown` | Not model-exposed; drift validation only |
| 9 | `get_agent_skill` | `id` | Not model-exposed; drift validation only |
| 10 | `get_attachment` | `id` | Not model-exposed; drift validation only |
| 11 | `get_diff` | `urlOrId` | Not model-exposed; drift validation only |
| 12 | `get_diff_threads` | `urlOrId` | Not model-exposed; drift validation only |
| 13 | `get_document` | `id` | Not model-exposed; drift validation only |
| 14 | `get_issue` | `id` | `linear_get_issue` |
| 15 | `get_issue_status` | `id, name, team` | Not model-exposed; drift validation only |
| 16 | `get_milestone` | `project, query` | Not model-exposed; drift validation only |
| 17 | `get_project` | `query` | Not model-exposed; drift validation only |
| 18 | `get_release` | `id` | Not model-exposed; drift validation only |
| 19 | `get_release_note` | `id` | Not model-exposed; drift validation only |
| 20 | `get_status_updates` | `type` | Not model-exposed; drift validation only |
| 21 | `get_team` | `query` | Not model-exposed; drift validation only. Team guards use authoritative GraphQL lookups. |
| 22 | `get_user` | `query` | Internal MCP↔GraphQL actor identity pin for every operation; not model-exposed |
| 23 | `list_agent_skills` | `—` | Not model-exposed; drift validation only |
| 24 | `list_comments` | `—` | Not model-exposed; drift validation only |
| 25 | `list_cycles` | `teamId` | Not model-exposed; drift validation only |
| 26 | `list_diffs` | `—` | Not model-exposed; drift validation only |
| 27 | `list_documents` | `—` | Not model-exposed; drift validation only |
| 28 | `list_issue_labels` | `—` | Not model-exposed; drift validation only |
| 29 | `list_issue_statuses` | `team` | Not model-exposed; drift validation only. Lifecycle guards use authoritative GraphQL lookups. |
| 30 | `list_issues` | `—` | linear_list_issues |
| 31 | `list_milestones` | `project` | Not model-exposed; drift validation only |
| 32 | `list_project_labels` | `—` | Not model-exposed; drift validation only |
| 33 | `list_projects` | `—` | Not model-exposed; drift validation only |
| 34 | `list_release_notes` | `—` | Not model-exposed; drift validation only |
| 35 | `list_release_pipelines` | `—` | Not model-exposed; drift validation only |
| 36 | `list_releases` | `—` | Not model-exposed; drift validation only |
| 37 | `list_teams` | `—` | Not model-exposed; drift validation only |
| 38 | `list_users` | `—` | Not model-exposed; drift validation only |
| 39 | `merge_diff` | `urlOrId` | Not model-exposed; drift validation only |
| 40 | `prepare_attachment_upload` | `issue, filename, contentType, size` | Not model-exposed; drift validation only |
| 41 | `resolve_diff_thread` | `threadId` | Not model-exposed; drift validation only |
| 42 | `save_comment` | `body` | linear_save_comment |
| 43 | `save_diff_comment` | `body` | Not model-exposed; drift validation only |
| 44 | `save_document` | `—` | Not model-exposed; drift validation only |
| 45 | `save_issue` | `—` | linear_save_issue |
| 46 | `save_milestone` | `project` | Not model-exposed; drift validation only |
| 47 | `save_project` | `—` | Not model-exposed; drift validation only |
| 48 | `save_release` | `—` | Not model-exposed; drift validation only |
| 49 | `save_release_note` | `—` | Not model-exposed; drift validation only |
| 50 | `save_status_update` | `type` | Not model-exposed; drift validation only |
| 51 | `search_documentation` | `query` | Not model-exposed; drift validation only |
| 52 | `submit_diff_review` | `urlOrId, decision` | Not model-exposed; drift validation only |

## Local contract decisions

| Surface | Decision | Evidence |
|---|---|---|
| `save_issue.project` | Explicit JSON `null` is accepted and forwarded unchanged, enabling project clearing. | `test_save_issue_forwards_explicit_project_null` |
| Project completion | No semantic tool is registered; unknown allowlist entry fails closed. Completion remains human-controlled. | `test_project_completion_capability_is_not_model_exposed` |
| Execution allowlist | Discovery does not confer execution authority. `call_tool` rejects every vendor tool outside the five required local contracts, including raw `save_project`. | `test_non_required_vendor_tool_cannot_be_called` |
| Explicit nulls | Only top-level `save_issue.project` may be explicitly null on the model surface; every other top-level or nested explicit null fails local policy before forwarding. | `test_only_project_accepts_explicit_null_on_model_surface`, `test_nested_explicit_null_is_rejected` |
| Schema drift | All 52 tool names and schema dialects are pinned. Exact property sets, requiredness and `additionalProperties` are pinned for the five required vendor contracts; type/nullability/format checks cover every locally forwarded field. | `test_mcp_client.py` |
| Initialize and tool discovery | The negotiated protocol must be supported; the server must advertise an object-valued tools capability and string-valued required server identity fields before `notifications/initialized` or `tools/list`. Invalid initialize branches leave protocol/session state uncommitted; optional and additional vendor metadata remains compatible. Tool discovery follows bounded cursor pagination with duplicate/loop/size guards. Each semantic operation creates a fresh MCP connection and re-runs discovery, so no persistent tool-list cache can outlive a vendor change; `listChanged` is type-checked but no redundant long-lived notification subscription is retained. | `test_missing_tools_capability_fails_before_initialized_notification`, `test_non_object_tools_capability_fails_closed`, `test_non_boolean_list_changed_capability_fails_closed`, `test_invalid_server_info_fails_closed`, `test_initialize_accepts_optional_and_additional_vendor_metadata`, `test_tools_list_pagination_collects_required_contract`, cursor limit tests |
| HTTP response media type | Only one well-formed `application/json` or `text/event-stream` header is accepted, optionally with exact quoted or unquoted `charset=utf-8`. A mutation response with invalid metadata is outcome-unknown. | `test_unexpected_response_content_type_fails_closed`, `test_malformed_content_type_parameter_fails_closed`, `test_asymmetrically_quoted_charset_fails_closed` |
| Session identifier | Negotiated-era `Mcp-Session-Id` values are accepted only from fully validated initialize responses, bounded to 1024 visible-ASCII bytes, and never replaced by subsequent responses. Empty, duplicate, invalid or unexpected mutation metadata is outcome-unknown. | `test_session_id_with_non_visible_ascii_fails_closed`, `test_oversized_session_id_fails_closed`, `test_invalid_initialize_envelope_does_not_commit_session_id`, `test_non_initialization_session_id_header_fails_closed`, `test_mutation_session_id_header_is_outcome_unknown`, `test_empty_mutation_session_header_is_outcome_unknown` |
| Mutation classification | Mutation semantics are derived from the authorized vendor tool name at both the execution/idempotency boundary and MCP transport boundary, not from a caller-controlled boolean. Any mismatch fails before identity lookup or dispatch. HTTP 401 may refresh the shared credential but never redispatches the same mutation. | `test_mutation_classification_mismatch_fails_before_identity_or_dispatch`, `test_mutation_classification_is_derived_from_tool_name`, `test_mutation_401_is_not_redispatched` |
| Ambiguous mutation outcome | No blind retry; mutation transport/RPC/result ambiguity becomes `outcome_unknown`. | `test_mcp_client.py`, `test_outbound_ledger.py` |
| OAuth | Shared profile-local store is the sole refresh owner abstraction; refresh is locked, rotated and atomically persisted. | `oauth_store.py` tests |
| AgentSession/webhook | HMAC, dedup, closure fencing, outbox order, dead-letter and recovery are regression-tested. | `test_native_platform.py` |

## Normalized live schema snapshot

- Read-only snapshot SHA-256: `61fb5ed96b1883d08438ed5f8d23c26dac03d093e77bd617cfa55f9a8454e9b6`.
- Tool count: `52`; tools publishing `outputSchema`: `0`.
- Input-schema root shapes: with or without `required`; both include `$schema`, `type`, `properties`, and `additionalProperties`.
- Live nullable fields (`17`): `list_issues.assignee`; `save_issue.assignee`, `cycle`, `delegate`, `dueDate`, `duplicateOf`, `estimate`, `parentId`, `project`, `slaBreachesAt`, `slaType`; `save_milestone.targetDate`; `save_project.lead`; `save_release.completedAt`, `startDate`, `startedAt`, `targetDate`.
- Local model policy intentionally accepts explicit null only for `save_issue.project`; every other model-surface null fails closed until a semantic clear operation is separately designed and reviewed.

## Runtime audit snapshot

- Lifecycle-hardening source commit `fa70d9fd43a1fcb07db095fae53c593db742af1b` and reviewed-manifest commit `f5e9461076dd78a425ee2105924883afc93c966b` are pushed. The source artifact is promoted and loaded across all nine profiles; deployable-file parity is `9/9 × 10/10`, plugin directories are `0700`, and deployable files are `0600`.
- The MCP client now publishes an executable protocol/session/tool contract only after provisional initialize, initialized notification, paginated discovery, and schema validation complete. Recursive session recovery is removed; read recovery has one explicit budget; every post-dispatch mutation ambiguity preserves `MCPOutcomeUnknown`, never redispatches, and best-effort invalidates the session even if OAuth or DELETE cleanup fails. Session DELETE refreshes once on `401` with pinned session/protocol headers.
- The lifecycle regression suite passes `307/307`; focused MCP tests pass `61/61`; `compileall`, `py_compile`, and `git diff --check` pass. Two independent final security/release reviews reported `BLOCKERS: NONE`.
- General exposes reviewed read tools plus `linear_save_issue` and `linear_save_comment`; eight specialist profiles expose reviewed read tools plus `linear_save_comment` only. Raw `save_project` and project completion remain absent from all nine model surfaces.
- Nine launchd gateway processes restarted after artifact promotion and each acquired a new PID. All nine Linear health endpoints report `status=ok`, `pending=0`, `in_flight=0`, and `dead=0`; all nine outbound ledgers pass `quick_check`. Fresh timestamped stdout shows each profile reconnecting to Telegram and its profile-owned Linear listener.
- General's post-restart canary authoritatively read `OPS-101` through the allowed official MCP contract. A request for forbidden `includeRelations` failed closed at local policy before the allowed read succeeded.
- The earlier approved `OPS-30` production canary remains the mutation/exactly-once evidence: top-level `save_issue.project=null` dispatched once, returned `result_id=OPS-30`, replayed the same namespaced operation key without a second vendor mutation, and authoritatively read back detached. No production mutation was dispatched during the `fa70d9f` lifecycle-hardening rollout.
- Previously observed launchd service-definition age remains a separate approval-gated fleet concern and was not folded into this Linear rollout.

## Rollback

Each approved promotion emitted an immutable per-profile rollback path and digest. Use only the recorded profile-specific artifact with `scripts/deploy_plugin.py rollback`; never select a backup by recency.
