"""Tests for BatonMiddleware per SPEC §11.2 + §11.5.

Strategy: use FastMCP's in-process Client to drive real tool calls through
a real BatonMiddleware-equipped FastMCP server. The middleware writes events
to an HttpSink pointing at pytest-httpserver; tests inspect what landed there.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.integrations.mcp.middleware import BatonMiddleware
from baton.sinks import HttpSink, Sink


@pytest.fixture
async def captured() -> list[dict[str, Any]]:
    """Per-test list that collects ingested event JSON bodies."""
    return []


@pytest.fixture
async def sink(
    httpserver: HTTPServer,
    captured: list[dict[str, Any]],
) -> Sink:
    def handler(request: Any) -> Response:
        captured.append(request.get_json())
        return Response("", status=201)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
    s = HttpSink(url=httpserver.url_for(""), api_key="k")
    yield s
    await s.aclose()


def _build_mcp(sink: Sink, **mw_kwargs: Any) -> FastMCP:
    mcp = FastMCP("test-vendor")
    mcp.add_middleware(
        BatonMiddleware(
            tenant_id="ten_test",
            consent_token="ct_test",
            sink=sink,
            **mw_kwargs,
        )
    )
    return mcp


# =============================================================================
# Success path — tool_call_start + tool_call_end
# =============================================================================


class TestSuccessfulToolCall:
    async def test_emits_start_and_end(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "hello"})

        await sink.flush()
        types = [ev["event_type"] for ev in captured]
        assert types == ["tool_call_start", "tool_call_end"]

    async def test_tool_name_and_params_in_start_event(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def add(a: int, b: int) -> int:
            return a + b

        async with Client(mcp) as client:
            await client.call_tool("add", {"a": 3, "b": 4})

        await sink.flush()
        start_event = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start_event["payload"]["tool_name"] == "add"
        assert start_event["payload"]["params"] == {"a": 3, "b": 4}

    async def test_result_in_end_event(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def double(n: int) -> int:
            return n * 2

        async with Client(mcp) as client:
            await client.call_tool("double", {"n": 7})

        await sink.flush()
        end_event = next(ev for ev in captured if ev["event_type"] == "tool_call_end")
        assert end_event["payload"]["tool_name"] == "double"
        # Result is whatever FastMCP serializes — at minimum non-None
        assert end_event["payload"]["result"] is not None
        assert isinstance(end_event["payload"]["duration_ms"], int)
        assert end_event["payload"]["duration_ms"] >= 0


# =============================================================================
# Failure path — tool_call_start + tool_call_error
# =============================================================================


class TestFailedToolCall:
    async def test_emits_start_and_error(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def boom() -> None:
            raise RuntimeError("kaboom")

        async with Client(mcp) as client:
            with pytest.raises(Exception):  # noqa: B017 fastmcp wraps; we only care that something raised
                await client.call_tool("boom", {})

        await sink.flush()
        types = [ev["event_type"] for ev in captured]
        assert "tool_call_start" in types
        assert "tool_call_error" in types
        assert "tool_call_end" not in types  # error path skips end event

    async def test_error_payload_fields(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def boom() -> None:
            raise ValueError("specific message")

        async with Client(mcp) as client:
            with pytest.raises(Exception):  # noqa: B017 fastmcp wraps; we only care that something raised
                await client.call_tool("boom", {})

        await sink.flush()
        error_event = next(ev for ev in captured if ev["event_type"] == "tool_call_error")
        # error_type captures the exception class name (FastMCP may wrap; either
        # the original or the wrapper class name is acceptable).
        assert error_event["payload"]["error_type"]
        assert "specific message" in error_event["payload"]["error_body"]
        assert isinstance(error_event["payload"]["duration_ms"], int)


# =============================================================================
# Sequence numbers
# =============================================================================


class TestSequenceNumbers:
    async def test_monotonic_per_session(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "1"})
            await client.call_tool("echo", {"text": "2"})
            await client.call_tool("echo", {"text": "3"})

        await sink.flush()
        # 3 tool calls x 2 events each = 6 events
        seqs = [ev["sequence_number"] for ev in captured]
        # Same session → must be strictly increasing
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs), "sequence numbers must be unique"
        assert seqs[0] == 1, "sequence starts at 1"

    async def test_start_seq_less_than_end_seq(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        end = next(ev for ev in captured if ev["event_type"] == "tool_call_end")
        assert start["sequence_number"] < end["sequence_number"]


# =============================================================================
# Tenant + envelope fields
# =============================================================================


class TestEnvelopeFields:
    async def test_tenant_id_set_correctly(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        for ev in captured:
            assert ev["tenant_id"] == "ten_test"

    async def test_session_id_same_across_events_within_call(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        session_ids = {ev["session_id"] for ev in captured}
        assert len(session_ids) == 1, "start + end of same tool call must share session_id"

    async def test_spec_and_sdk_versions_present(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        for ev in captured:
            assert ev["sdk_version"].startswith("0.1.")

    async def test_agent_runtime_default_unknown(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """When no _meta is supplied and no explicit override, agent_runtime
        defaults to 'unknown'."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        for ev in captured:
            assert ev["agent_runtime"] == "unknown"

    async def test_explicit_default_agent_runtime(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """Middleware accepts a `default_agent_runtime` constructor arg for
        deployments where the runtime is known up front (e.g., shipping
        specifically into a Claude Code plugin)."""
        mcp = _build_mcp(sink, default_agent_runtime="claude-code")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        for ev in captured:
            assert ev["agent_runtime"] == "claude-code"

    async def test_detects_claude_code_from_meta(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """When the client supplies ``_meta.claudecode/toolUseId``, the
        middleware MUST detect ``claude-code`` — not fall back to the
        default. Regression: FastMCP 3.x strips ``_meta`` from the
        middleware's ``CallToolRequestParams``; meta must be read from
        ``fastmcp_context.request_context.meta`` instead."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo",
                {"text": "x"},
                meta={"claudecode/toolUseId": "tool-use-abc-123"},
            )

        await sink.flush()
        assert captured, "no events captured"
        for ev in captured:
            assert ev["agent_runtime"] == "claude-code"

    async def test_explicit_baton_override_in_meta(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """``_meta.baton.agent_runtime`` takes precedence over heuristics."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo",
                {"text": "x"},
                meta={"baton": {"agent_runtime": "my-custom-runtime"}},
            )

        await sink.flush()
        for ev in captured:
            assert ev["agent_runtime"] == "my-custom-runtime"
