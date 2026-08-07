"""Tests for per-tool intent-param injection in the FastMCP middleware.

Mirrors baton-extmcp's vendor-neutral intent-injection matrix, adapted to the
in-process FastMCP Client harness: inject ``user_goal``/``expected_result`` on
``tools/list``, strip both on ``tools/call``, ride ``user_goal`` on
``tool_call_start.payload.call_intent``, and synthesise one proactive
annotation (carrying ``expected_result`` too, if present) from the session's
first injected intent.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.integrations._llm_text import (
    EXPECTED_RESULT_PARAM_NAME,
    INTENT_SOURCE_PARAM,
    USER_GOAL_PARAM_NAME,
)
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
        assert USER_GOAL_PARAM_NAME in props
        assert props[USER_GOAL_PARAM_NAME]["type"] == "string"
        # optional → NOT added to required
        assert USER_GOAL_PARAM_NAME not in echo_tool.inputSchema.get("required", [])

    async def test_required_mode_adds_to_required(self, sink: Sink) -> None:
        mcp = _build_mcp(sink, intent_param_mode="required")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        assert USER_GOAL_PARAM_NAME in echo_tool.inputSchema["properties"]
        assert USER_GOAL_PARAM_NAME in echo_tool.inputSchema["required"]

    async def test_off_mode_no_injection(self, sink: Sink) -> None:
        mcp = _build_mcp(sink, intent_param_mode="off")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        assert USER_GOAL_PARAM_NAME not in echo_tool.inputSchema.get("properties", {})

    async def test_native_param_left_untouched(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """A tool that already declares ``user_goal`` keeps its own — the
        injector records it ``native`` and the caller's value is forwarded, not
        stripped."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str, user_goal: str = "") -> str:
            # Vendor's own param — echo it back so the test can assert it arrived.
            return f"{text}|{user_goal}"

        async with Client(mcp) as client:
            result = await client.call_tool("echo", {"text": "x", "user_goal": "vendor-value"})

        await sink.flush()
        # forwarded to the vendor handler (not stripped)
        assert "vendor-value" in str(result.content[0].text)  # type: ignore[union-attr]
        # and never captured as call_intent (disposition = native)
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"].get("call_intent") is None

    async def test_expected_result_injected_optional_even_in_required_mode(
        self, sink: Sink
    ) -> None:
        """``required`` mode escalates only ``user_goal`` — ``expected_result``
        stays optional regardless (a bigger surface mutation than the signal
        warrants)."""
        mcp = _build_mcp(sink, intent_param_mode="required")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        assert EXPECTED_RESULT_PARAM_NAME in echo_tool.inputSchema["properties"]
        assert EXPECTED_RESULT_PARAM_NAME not in echo_tool.inputSchema.get("required", [])

    async def test_native_expected_result_left_untouched_independently(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """A tool with its own ``expected_result`` param is forwarded untouched
        for that param, while ``user_goal`` injection still proceeds normally —
        dispositions are tracked per param, not per tool."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str, expected_result: str = "") -> str:
            return f"{text}|{expected_result}"

        async with Client(mcp) as client:
            tools = await client.list_tools()
            (echo_tool,) = tools
            assert USER_GOAL_PARAM_NAME in echo_tool.inputSchema["properties"]

            result = await client.call_tool(
                "echo",
                {
                    "text": "x",
                    "expected_result": "vendor-value",
                    USER_GOAL_PARAM_NAME: "why the user called",
                },
            )

        await sink.flush()
        # vendor's own expected_result forwarded, not stripped
        assert "vendor-value" in str(result.content[0].text)  # type: ignore[union-attr]
        # user_goal still stripped + captured normally
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"]["call_intent"] == "why the user called"


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
            # If user_goal leaked to the handler, FastMCP would have rejected
            # the call (unexpected kwarg) — reaching here proves it was stripped.
            seen["text"] = text
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo", {"text": "hello", USER_GOAL_PARAM_NAME: "user wants a greeting"}
            )

        await sink.flush()
        assert seen == {"text": "hello"}  # vendor handler never saw user_goal
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"]["call_intent"] == "user wants a greeting"
        assert start["payload"]["intent_source"] == INTENT_SOURCE_PARAM
        # params captured == vendor-visible args, no user_goal
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
            await client.call_tool("echo", {"text": "x", USER_GOAL_PARAM_NAME: "why the user called"})

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
            await client.call_tool("echo", {"text": "1", USER_GOAL_PARAM_NAME: "first why"})
            await client.call_tool("echo", {"text": "2", USER_GOAL_PARAM_NAME: "second why"})

        await sink.flush()
        annotations = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(annotations) == 1, "only the session's first intent synthesises a proactive"
        # but the second call still rides its intent on the start event
        starts = [ev for ev in captured if ev["event_type"] == "tool_call_start"]
        assert starts[1]["payload"]["call_intent"] == "second why"

    async def test_expected_result_rides_proactive_only_not_start(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """``expected_result`` feeds the proactive annotation's
        ``expected_outcome`` (mirrors baton-extmcp) — it does not ride
        ``tool_call_start``, which only ever carries ``call_intent``."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo",
                {
                    "text": "x",
                    USER_GOAL_PARAM_NAME: "why the user called",
                    EXPECTED_RESULT_PARAM_NAME: "a successful echo",
                },
            )

        await sink.flush()
        ann = next(ev for ev in captured if ev["event_type"] == "annotation")
        assert ann["payload"]["expected_outcome"] == "a successful echo"
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert "expected_outcome" not in start["payload"]
        assert "expected_result" not in start["payload"]["params"]
