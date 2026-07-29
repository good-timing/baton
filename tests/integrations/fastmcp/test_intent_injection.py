"""Tests for per-tool intent-param injection in the FastMCP middleware.

Mirrors baton-proxy's intent-injection matrix, adapted to the in-process
FastMCP Client harness: inject on ``tools/list``, strip on ``tools/call``, ride
the value on ``tool_call_start.payload.call_intent``, and synthesise one
proactive annotation from the session's first injected intent.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.integrations._llm_text import INTENT_PARAM_NAME, INTENT_SOURCE_PARAM
from baton.integrations.fastmcp.middleware import BatonMiddleware
from baton.sinks import HttpSink, Sink


@pytest.fixture
async def captured() -> list[dict[str, Any]]:
    return []


@pytest.fixture
async def sink(httpserver: HTTPServer, captured: list[dict[str, Any]]) -> Sink:
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
            vendor_id="ten_test",
            consent_token="ct_test",
            sink=sink,
            **mw_kwargs,
        )
    )
    return mcp


# =============================================================================
# tools/list injection
# =============================================================================


class TestListInjection:
    async def test_optional_mode_injects_param(self, sink: Sink) -> None:
        mcp = _build_mcp(sink)  # default mode = optional

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        props = echo_tool.inputSchema["properties"]
        assert INTENT_PARAM_NAME in props
        assert props[INTENT_PARAM_NAME]["type"] == "string"
        # optional → NOT added to required
        assert INTENT_PARAM_NAME not in echo_tool.inputSchema.get("required", [])

    async def test_required_mode_adds_to_required(self, sink: Sink) -> None:
        mcp = _build_mcp(sink, intent_param_mode="required")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        assert INTENT_PARAM_NAME in echo_tool.inputSchema["properties"]
        assert INTENT_PARAM_NAME in echo_tool.inputSchema["required"]

    async def test_off_mode_no_injection(self, sink: Sink) -> None:
        mcp = _build_mcp(sink, intent_param_mode="off")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        assert INTENT_PARAM_NAME not in echo_tool.inputSchema.get("properties", {})

    async def test_native_param_left_untouched(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """A tool that already declares ``baton_intent`` keeps its own — the
        injector records it ``native`` and the caller's value is forwarded, not
        stripped."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str, baton_intent: str = "") -> str:
            # Vendor's own param — echo it back so the test can assert it arrived.
            return f"{text}|{baton_intent}"

        async with Client(mcp) as client:
            result = await client.call_tool("echo", {"text": "x", "baton_intent": "vendor-value"})

        await sink.flush()
        # forwarded to the vendor handler (not stripped)
        assert "vendor-value" in str(result.content[0].text)  # type: ignore[union-attr]
        # and never captured as call_intent (disposition = native)
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"].get("call_intent") is None


# =============================================================================
# tools/call strip + capture
# =============================================================================


class TestCallStripAndCapture:
    async def test_strips_intent_and_captures_call_intent(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        seen: dict[str, Any] = {}
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            # If baton_intent leaked to the handler, FastMCP would have rejected
            # the call (unexpected kwarg) — reaching here proves it was stripped.
            seen["text"] = text
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo", {"text": "hello", INTENT_PARAM_NAME: "user wants a greeting"}
            )

        await sink.flush()
        assert seen == {"text": "hello"}  # vendor handler never saw baton_intent
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"]["call_intent"] == "user wants a greeting"
        assert start["payload"]["intent_source"] == INTENT_SOURCE_PARAM
        # params captured == vendor-visible args, no baton_intent
        assert start["payload"]["params"] == {"text": "hello"}

    async def test_no_intent_leaves_call_intent_null(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "hi"})

        await sink.flush()
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"].get("call_intent") is None
        assert start["payload"].get("intent_source") is None


# =============================================================================
# proactive synthesis + dedup
# =============================================================================


class TestProactiveSynthesis:
    async def test_first_intent_emits_proactive_before_start(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x", INTENT_PARAM_NAME: "why the user called"})

        await sink.flush()
        types = [ev["event_type"] for ev in captured]
        assert types[0] == "annotation", "proactive must be sequenced first"
        ann = captured[0]
        assert ann["payload"]["intent"] == "why the user called"
        assert ann["payload"]["intent_source"] == INTENT_SOURCE_PARAM
        assert ann["payload"]["tool_name"] == "echo"
        # sequence: annotation < start
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert ann["sequence_number"] < start["sequence_number"]

    async def test_only_one_proactive_per_session(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "1", INTENT_PARAM_NAME: "first why"})
            await client.call_tool("echo", {"text": "2", INTENT_PARAM_NAME: "second why"})

        await sink.flush()
        annotations = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(annotations) == 1, "only the session's first intent synthesises a proactive"
        # but the second call still rides its intent on the start event
        starts = [ev for ev in captured if ev["event_type"] == "tool_call_start"]
        assert starts[1]["payload"]["call_intent"] == "second why"
