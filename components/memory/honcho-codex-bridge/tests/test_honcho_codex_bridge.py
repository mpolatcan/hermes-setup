from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from honcho_codex_adapter.app import create_app
from honcho_codex_adapter.driver import (
    AdapterError,
    DriverResult,
    HermesCodexDriver,
    build_upstream_kwargs,
    enforce_output_limit,
    validate_upstream_tool_calls,
)
from honcho_codex_adapter.models import ChatCompletionRequest
from honcho_codex_adapter.scheduler import WeightedPriorityScheduler, queue_class_for_model

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from honcho_dream_tool_canary import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    DEDUCTION_ROUTE,
    EXPECTED_TOOL_NAMES,
    INDUCTION_ROUTE,
    ToolCase,
    build_cases,
    build_payload,
    validate_catalogs,
    validate_response,
)


class FakeDriver:
    def complete(
        self,
        request: ChatCompletionRequest,
        cancel_event: threading.Event | None = None,
    ) -> DriverResult:
        return DriverResult(
            id="chatcmpl-test", created=1, model=request.model, content='{"answer":"ok"}',
            tool_calls=None, finish_reason="stop",
            usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        )


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(FakeDriver(), api_key="test-secret"))
        self.headers = {"Authorization": "Bearer test-secret"}

    def test_health(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

    def test_auth_required(self):
        self.assertEqual(self.client.get("/v1/models").status_code, 401)

    def test_queue_full_is_bounded_and_explicit(self):
        app = create_app(driver=FakeDriver(), api_key="test-key", queue_capacity=1)
        app.state.scheduler.try_acquire = AsyncMock(return_value=None)
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={"model": "honcho-deriver", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "queue_full")

    def test_unknown_model_rejected_by_translation_layer(self):
        request = ChatCompletionRequest.model_validate({
            "model": "forbidden", "messages": [{"role": "user", "content": "hi"}],
        })
        with self.assertRaisesRegex(Exception, "model is not allowed"):
            build_upstream_kwargs(request, "test-session")

    def test_non_stream_response(self):
        payload = {"model": "honcho-deriver", "messages": [{"role": "user", "content": "hi"}]}
        response = self.client.post("/v1/chat/completions", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["choices"][0]["message"]["content"], '{"answer":"ok"}')
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["usage"]["total_tokens"], 5)

    def test_final_only_stream(self):
        payload = {
            "model": "honcho-deriver", "messages": [{"role": "user", "content": "hi"}],
            "stream": True, "stream_options": {"include_usage": True},
        }
        response = self.client.post("/v1/chat/completions", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("data: [DONE]", response.text)
        self.assertIn('"finish_reason":"stop"', response.text)
        self.assertIn('"total_tokens":5', response.text)

    def test_adapter_error_status_and_code_are_preserved(self):
        class ErrorDriver:
            def complete(
                self,
                request: ChatCompletionRequest,
                cancel_event: threading.Event | None = None,
            ) -> DriverResult:
                raise AdapterError(
                    "Codex upstream request timed out",
                    status_code=504,
                    code="timeout_error",
                )

        app = create_app(ErrorDriver(), api_key="test-key")
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "honcho-deriver",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["code"], "timeout_error")

    def test_structured_output_translation(self):
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-deriver",
            "messages": [{"role": "system", "content": "Return JSON"}, {"role": "user", "content": "go"}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "answer", "strict": True,
                "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False},
            }},
        })
        kwargs = build_upstream_kwargs(request, "test-session")
        self.assertEqual(kwargs["text"]["format"]["type"], "json_schema")
        self.assertEqual(kwargs["text"]["format"]["name"], "answer")
        self.assertNotIn("max_output_tokens", kwargs)

    def test_max_completion_tokens_is_not_forwarded(self):
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-deriver",
            "messages": [{"role": "user", "content": "brief"}],
            "max_completion_tokens": 128,
        })
        kwargs = build_upstream_kwargs(request, "test-session")
        self.assertNotIn("max_output_tokens", kwargs)

    def test_plain_text_output_is_truncated_at_requested_token_cap(self):
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-deriver",
            "messages": [{"role": "user", "content": "brief"}],
            "max_completion_tokens": 250,
        })
        result = DriverResult(
            id="chatcmpl-long", created=1, model=request.model,
            content="word " * 600, tool_calls=None, finish_reason="stop",
            usage={"prompt_tokens": 2, "completion_tokens": 600, "total_tokens": 602},
        )
        bounded = enforce_output_limit(result, request)
        from honcho_codex_adapter.driver import OUTPUT_ENCODING

        self.assertLessEqual(
            len(OUTPUT_ENCODING.encode(bounded.content or "", disallowed_special=())),
            250,
        )
        self.assertEqual(bounded.finish_reason, "length")
        self.assertEqual(bounded.usage["completion_tokens"], 600)

    def test_structured_and_tool_outputs_fail_closed_when_over_cap(self):
        structured = ChatCompletionRequest.model_validate({
            "model": "honcho-deriver",
            "messages": [{"role": "user", "content": "brief"}],
            "max_completion_tokens": 8,
            "response_format": {"type": "json_object"},
        })
        tool_request = ChatCompletionRequest.model_validate({
            "model": "honcho-dialectic",
            "messages": [{"role": "user", "content": "search"}],
            "max_completion_tokens": 8,
        })
        cases = (
            (structured, DriverResult(
                id="structured", created=1, model=structured.model,
                content=json.dumps({"answer": "word " * 100}), tool_calls=None,
                finish_reason="stop", usage={},
            )),
            (tool_request, DriverResult(
                id="tool", created=1, model=tool_request.model, content=None,
                tool_calls=[{"id": "call_1", "type": "function", "function": {
                    "name": "search_memory", "arguments": json.dumps({"query": "word " * 100}),
                }}], finish_reason="tool_calls", usage={},
            )),
        )
        for payload, result in cases:
            with self.subTest(result=result.id):
                with self.assertRaises(AdapterError) as caught:
                    enforce_output_limit(result, payload)
                self.assertEqual(caught.exception.code, "output_limit_exceeded")

    def test_tool_translation_and_specific_choice(self):
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-dialectic",
            "messages": [{"role": "user", "content": "search"}],
            "tools": [{"type": "function", "function": {"name": "search_memory", "description": "search", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}],
            "tool_choice": {"type": "function", "function": {"name": "search_memory"}},
        })
        kwargs = build_upstream_kwargs(request, "test-session")
        self.assertEqual(kwargs["tools"][0]["name"], "search_memory")
        self.assertEqual(kwargs["tool_choice"], {"type": "function", "name": "search_memory"})

    def test_specific_choice_must_reference_declared_tool(self):
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-dialectic",
            "messages": [{"role": "user", "content": "search"}],
            "tools": [{"type": "function", "function": {
                "name": "search_memory", "description": "search",
                "parameters": {"type": "object", "properties": {}},
            }}],
            "tool_choice": {"type": "function", "function": {"name": "missing"}},
        })
        with self.assertRaisesRegex(AdapterError, "unknown tool"):
            build_upstream_kwargs(request, "test-session")

    def test_duplicate_tool_names_are_rejected(self):
        tool = {"type": "function", "function": {
            "name": "search_memory", "description": "search",
            "parameters": {"type": "object", "properties": {}},
        }}
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-dialectic",
            "messages": [{"role": "user", "content": "search"}],
            "tools": [tool, tool],
        })
        with self.assertRaisesRegex(AdapterError, "duplicate tool name"):
            build_upstream_kwargs(request, "test-session")

    def test_upstream_tool_arguments_are_schema_validated(self):
        tools = [{"type": "function", "function": {
            "name": "search_memory", "description": "search",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }}]
        valid = validate_upstream_tool_calls(
            [SimpleNamespace(id="call_1", name="search_memory", arguments='{"query":"alpha"}')],
            tools,
        )
        self.assertEqual(valid[0]["function"]["name"], "search_memory")
        with self.assertRaisesRegex(AdapterError, "schema-invalid"):
            validate_upstream_tool_calls(
                [SimpleNamespace(id="call_2", name="search_memory", arguments='{"query":3}')],
                tools,
            )

    def test_raw_upstream_errors_are_mapped_and_credentials_refresh(self):
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-deriver",
            "messages": [{"role": "user", "content": "hi"}],
        })

        class StatusError(RuntimeError):
            def __init__(self, status_code: int) -> None:
                super().__init__(f"status {status_code}")
                self.status_code = status_code

        cases = (
            (StatusError(401), 401, "authentication_error"),
            (StatusError(429), 429, "rate_limit_error"),
            (TimeoutError("slow upstream"), 504, "timeout_error"),
        )
        for upstream_error, expected_status, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                client = MagicMock()
                client.responses.create.side_effect = upstream_error
                resolve = MagicMock(return_value={
                    "api_key": "codex-token",
                    "base_url": "https://example.invalid",
                })
                with (
                    patch("honcho_codex_adapter.driver.build_upstream_kwargs", return_value={}),
                    patch("openai.OpenAI", return_value=client),
                    patch("hermes_cli.auth.resolve_codex_runtime_credentials", resolve),
                    patch("agent.auxiliary_client._codex_cloudflare_headers", return_value={}),
                ):
                    with self.assertRaises(AdapterError) as caught:
                        HermesCodexDriver().complete(request)
                self.assertEqual(caught.exception.status_code, expected_status)
                self.assertEqual(caught.exception.code, expected_code)
                resolve.assert_called_once_with(refresh_if_expiring=True)
                client.close.assert_called_once_with()

    def test_upstream_timeout_and_retry_policy_is_bounded_and_configurable(self):
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-deriver",
            "messages": [{"role": "user", "content": "hi"}],
        })
        client = MagicMock()
        client.responses.create.side_effect = TimeoutError("slow upstream")
        openai = MagicMock(return_value=client)
        with (
            patch("honcho_codex_adapter.driver.build_upstream_kwargs", return_value={}),
            patch("openai.OpenAI", openai),
            patch(
                "hermes_cli.auth.resolve_codex_runtime_credentials",
                return_value={
                    "api_key": "codex-token",
                    "base_url": "https://example.invalid",
                },
            ),
            patch("agent.auxiliary_client._codex_cloudflare_headers", return_value={}),
        ):
            with self.assertRaises(AdapterError):
                HermesCodexDriver(
                    upstream_timeout_seconds=45,
                    upstream_max_retries=0,
                ).complete(request)

        self.assertEqual(openai.call_args.kwargs["timeout"], 45)
        self.assertEqual(openai.call_args.kwargs["max_retries"], 0)

        with patch.dict(
            os.environ,
            {
                "HONCHO_CODEX_UPSTREAM_TIMEOUT_SECONDS": "bad",
                "HONCHO_CODEX_UPSTREAM_MAX_RETRIES": "0",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "must be a number"):
                HermesCodexDriver()

    def test_event_stream_and_client_close_on_consume_failure(self):
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-deriver",
            "messages": [{"role": "user", "content": "hi"}],
        })
        event_stream = MagicMock()
        client = MagicMock()
        client.responses.create.return_value = event_stream
        with (
            patch("honcho_codex_adapter.driver.build_upstream_kwargs", return_value={}),
            patch("openai.OpenAI", return_value=client),
            patch(
                "hermes_cli.auth.resolve_codex_runtime_credentials",
                return_value={
                    "api_key": "codex-token",
                    "base_url": "https://example.invalid",
                },
            ),
            patch("agent.auxiliary_client._codex_cloudflare_headers", return_value={}),
            patch(
                "agent.codex_runtime._consume_codex_event_stream",
                side_effect=RuntimeError("consume failed"),
            ),
        ):
            with self.assertRaises(AdapterError) as caught:
                HermesCodexDriver().complete(request)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(caught.exception.code, "upstream_error")
        event_stream.close.assert_called_once_with()
        client.close.assert_called_once_with()

    def test_cancellation_reaches_stream_consumer_and_closes_resources(self):
        request = ChatCompletionRequest.model_validate({
            "model": "honcho-deriver",
            "messages": [{"role": "user", "content": "hi"}],
        })
        cancel_event = threading.Event()
        cancel_event.set()
        event_stream = MagicMock()
        client = MagicMock()
        client.responses.create.return_value = event_stream

        def consume(_stream, *, model, interrupt_check):
            self.assertEqual(model, "gpt-5.6-luna")
            self.assertTrue(interrupt_check())
            return SimpleNamespace()

        with (
            patch("honcho_codex_adapter.driver.build_upstream_kwargs", return_value={}),
            patch("openai.OpenAI", return_value=client),
            patch(
                "hermes_cli.auth.resolve_codex_runtime_credentials",
                return_value={
                    "api_key": "codex-token",
                    "base_url": "https://example.invalid",
                },
            ),
            patch("agent.auxiliary_client._codex_cloudflare_headers", return_value={}),
            patch("agent.codex_runtime._consume_codex_event_stream", side_effect=consume),
        ):
            with self.assertRaises(AdapterError) as caught:
                HermesCodexDriver().complete(
                    request,
                    cancel_event,
                )
        self.assertEqual(caught.exception.status_code, 499)
        self.assertEqual(caught.exception.code, "client_disconnected")
        event_stream.close.assert_called_once_with()
        client.close.assert_called_once_with()


class PrioritySchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_workload_routes_map_to_priority_classes(self):
        self.assertEqual(queue_class_for_model("honcho-dialectic"), "dialectic")
        self.assertEqual(queue_class_for_model("honcho-summary"), "summary")
        self.assertEqual(queue_class_for_model("honcho-deriver"), "deriver")
        self.assertEqual(queue_class_for_model("honcho-dream"), "dream")

    async def test_interactive_overtakes_queued_background(self):
        scheduler = WeightedPriorityScheduler(3)
        active = await scheduler.try_acquire("honcho-deriver")
        self.assertIsNotNone(active)
        dream_task = asyncio.create_task(
            scheduler.try_acquire("honcho-dream")
        )
        await asyncio.sleep(0)
        dialectic_task = asyncio.create_task(
            scheduler.try_acquire("honcho-dialectic")
        )
        await asyncio.sleep(0)

        await active.release()
        dialectic = await asyncio.wait_for(dialectic_task, timeout=0.1)
        self.assertIsNotNone(dialectic)
        self.assertFalse(dream_task.done())
        await dialectic.release()
        dream = await asyncio.wait_for(dream_task, timeout=0.1)
        self.assertIsNotNone(dream)
        await dream.release()

    async def test_same_class_is_fifo(self):
        scheduler = WeightedPriorityScheduler(3)
        active = await scheduler.try_acquire("honcho-deriver")
        first_task = asyncio.create_task(
            scheduler.try_acquire("honcho-summary")
        )
        await asyncio.sleep(0)
        second_task = asyncio.create_task(
            scheduler.try_acquire("honcho-summary")
        )
        await asyncio.sleep(0)

        await active.release()
        first = await asyncio.wait_for(first_task, timeout=0.1)
        self.assertFalse(second_task.done())
        await first.release()
        second = await asyncio.wait_for(second_task, timeout=0.1)
        await second.release()

    async def test_weighted_cycle_services_lower_class(self):
        scheduler = WeightedPriorityScheduler(11)
        active = await scheduler.try_acquire("honcho-deriver")
        dialectic_tasks = [
            asyncio.create_task(scheduler.try_acquire("honcho-dialectic"))
            for _ in range(9)
        ]
        summary_task = asyncio.create_task(
            scheduler.try_acquire("honcho-summary")
        )
        await asyncio.sleep(0)

        await active.release()
        for index in range(8):
            lease = await asyncio.wait_for(dialectic_tasks[index], timeout=0.1)
            self.assertFalse(summary_task.done())
            await lease.release()
        summary = await asyncio.wait_for(summary_task, timeout=0.1)
        self.assertFalse(dialectic_tasks[8].done())
        await summary.release()
        final_dialectic = await asyncio.wait_for(dialectic_tasks[8], timeout=0.1)
        await final_dialectic.release()

    async def test_aging_boost_prevents_starvation(self):
        now = [0.0]
        scheduler = WeightedPriorityScheduler(
            3,
            aging_seconds=30,
            clock=lambda: now[0],
        )
        active = await scheduler.try_acquire("honcho-deriver")
        dream_task = asyncio.create_task(
            scheduler.try_acquire("honcho-dream")
        )
        await asyncio.sleep(0)
        now[0] = 31.0
        dialectic_task = asyncio.create_task(
            scheduler.try_acquire("honcho-dialectic")
        )
        await asyncio.sleep(0)

        await active.release()
        dream = await asyncio.wait_for(dream_task, timeout=0.1)
        self.assertFalse(dialectic_task.done())
        await dream.release()
        dialectic = await asyncio.wait_for(dialectic_task, timeout=0.1)
        await dialectic.release()

    async def test_cancelled_waiter_releases_capacity(self):
        scheduler = WeightedPriorityScheduler(2)
        active = await scheduler.try_acquire("honcho-deriver")
        cancelled_task = asyncio.create_task(
            scheduler.try_acquire("honcho-dream")
        )
        await asyncio.sleep(0)
        cancelled_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled_task

        replacement_task = asyncio.create_task(
            scheduler.try_acquire("honcho-dialectic")
        )
        await asyncio.sleep(0)
        self.assertFalse(replacement_task.done())
        await active.release()
        replacement = await asyncio.wait_for(replacement_task, timeout=0.1)
        await replacement.release()

    async def test_capacity_counts_active_plus_waiting(self):
        scheduler = WeightedPriorityScheduler(1)
        active = await scheduler.try_acquire("honcho-deriver")
        rejected = await scheduler.try_acquire("honcho-dialectic")
        self.assertIsNone(rejected)
        await active.release()


class DreamToolCanaryTests(unittest.TestCase):
    @staticmethod
    def catalogs():
        schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        }
        return {
            specialist: [
                {
                    "name": name,
                    "description": f"{name} description",
                    "input_schema": schema,
                }
                for name in names
            ]
            for specialist, names in EXPECTED_TOOL_NAMES.items()
        }

    def test_catalog_counts_names_and_conversion(self):
        catalogs = validate_catalogs(self.catalogs())
        self.assertEqual(len(catalogs["deduction"]), 6)
        self.assertEqual(len(catalogs["induction"]), 4)
        self.assertEqual(
            [tool["function"]["name"] for tool in catalogs["deduction"]],
            EXPECTED_TOOL_NAMES["deduction"],
        )

    def test_catalog_drift_fails_closed(self):
        catalogs = self.catalogs()
        catalogs["deduction"].pop()
        with self.assertRaisesRegex(ValueError, "catalog drift"):
            validate_catalogs(catalogs)

    def test_build_cases_uses_shared_dream_route(self):
        cases = build_cases(validate_catalogs(self.catalogs()))
        self.assertEqual(len(cases), 10)
        self.assertEqual({case.model for case in cases[:6]}, {DEDUCTION_ROUTE})
        self.assertEqual({case.model for case in cases[6:]}, {INDUCTION_ROUTE})

    def test_payload_and_response_are_envelope_only(self):
        case = ToolCase(
            specialist="deduction",
            model=DEDUCTION_ROUTE,
            target="search_memory",
            tools=validate_catalogs(self.catalogs())["deduction"],
            arguments={"query": "sandbox canary", "top_k": 2},
        )
        payload = build_payload(case)
        self.assertEqual(payload["tool_choice"]["function"]["name"], case.target)
        self.assertFalse(any(message["role"] == "tool" for message in payload["messages"]))
        response = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-canary",
                                "type": "function",
                                "function": {
                                    "name": case.target,
                                    "arguments": json.dumps(case.arguments),
                                },
                            }
                        ]
                    },
                }
            ],
            "usage": {"total_tokens": 10},
        }
        result = validate_response(case, response)
        self.assertEqual(result["arguments"], case.arguments)

        response["choices"][0]["message"]["tool_calls"][0]["function"][
            "name"
        ] = "unknown_tool"
        with self.assertRaisesRegex(ValueError, "Expected tool search_memory"):
            validate_response(case, response)


if __name__ == "__main__":
    unittest.main()
