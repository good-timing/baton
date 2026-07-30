"""Tests for per-tool intent-param injection in the official-mcp-SDK adapter.

Mirrors ``tests/integrations/fastmcp/test_intent_injection.py`` but targets the
official ``mcp.server.fastmcp.FastMCP``: the adapter has no ``on_list_tools``
middleware hook, so it injects ``baton_intent`` into each ``Tool.parameters``
dict at install time (that dict is what ``FastMCP.list_tools`` advertises as
``inputSchema``) and strips it in the wrapped ``Tool.run``. Same contract as the
middleware — inject on list, strip on call, ride ``call_intent`` on
``tool_call_start``, synthesise one proactive per session.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from baton.integrations._llm_text import INTENT_PARAM_NAME, INTENT_SOURCE_PARAM
from baton.integrations.mcp import VendorConfig, install_baton
from baton.integrations.mcp._compat import MCPServerClass as FastMCP
from baton.sinks import FileSink


def _input_schema(tool: Any) -> dict[str, Any]:
    """The tool's advertised input JSON schema, across the mcp 1.x/2.0 rename.

    mcp 2.0 renamed the Python attr ``Tool.inputSchema`` → ``input_schema`` but
    kept the wire alias ``inputSchema``; dumping by alias reads the same on both.
    """
    return tool.model_dump(by_alias=True)["inputSchema"]


@pytest.fixture
def events_path(tmp_path: Any) -> str:
    return str(tmp_path / "events.jsonl")


def _install(events_path: str, **overrides: Any) -> tuple[Any, Any]:
    mcp = FastMCP("test-vendor-mcp")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-vendor",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(events_path),
            **overrides,
        ),
    )
    return mcp, handle


def _read_events(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# =============================================================================
# tools/list injection
# =============================================================================


class TestListInjection:
    async def test_optional_mode_injects_param(self, events_path: str) -> None:
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:
            tools = await mcp.list_tools()
            echo_tool = next(t for t in tools if t.name == "echo")
            props = _input_schema(echo_tool)["properties"]
            assert INTENT_PARAM_NAME in props
            assert props[INTENT_PARAM_NAME]["type"] == "string"
            # optional → NOT added to required
            assert INTENT_PARAM_NAME not in _input_schema(echo_tool).get("required", [])
        finally:
            await handle.aclose()

    async def test_required_mode_adds_to_required(self, events_path: str) -> None:
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(events_path),
                intent_param_mode="required",
            ),
        )
        try:
            tools = await mcp.list_tools()
            echo_tool = next(t for t in tools if t.name == "echo")
            assert INTENT_PARAM_NAME in _input_schema(echo_tool)["properties"]
            assert INTENT_PARAM_NAME in _input_schema(echo_tool)["required"]
        finally:
            await handle.aclose()

    async def test_off_mode_no_injection(self, events_path: str) -> None:
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(events_path),
                intent_param_mode="off",
            ),
        )
        try:
            tools = await mcp.list_tools()
            echo_tool = next(t for t in tools if t.name == "echo")
            assert INTENT_PARAM_NAME not in _input_schema(echo_tool).get("properties", {})
        finally:
            await handle.aclose()

    async def test_annotation_tool_not_injected(self, events_path: str) -> None:
        """The annotation tool takes ``intent`` explicitly — no redundant
        ``baton_intent`` should be injected into its schema."""
        mcp, handle = _install(events_path)
        try:
            tools = await mcp.list_tools()
            annotate = next(t for t in tools if t.name == "test-vendor_annotate")
            assert INTENT_PARAM_NAME not in _input_schema(annotate).get("properties", {})
        finally:
            await handle.aclose()

    async def test_post_install_tool_gets_injected(self, events_path: str) -> None:
        """A tool registered AFTER install_baton is injected via the patched
        add_tool, not just wrapped."""
        mcp, handle = _install(events_path)
        try:

            @mcp.tool()
            def post(x: int) -> int:
                return x * 2

            tools = await mcp.list_tools()
            post_tool = next(t for t in tools if t.name == "post")
            assert INTENT_PARAM_NAME in _input_schema(post_tool)["properties"]
        finally:
            await handle.aclose()

    async def test_native_param_left_untouched(self, events_path: str) -> None:
        """A tool that already declares ``baton_intent`` keeps its own — the
        injector records it ``native`` and the caller's value is forwarded, not
        stripped."""
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def echo(text: str, baton_intent: str = "") -> str:
            return f"{text}|{baton_intent}"

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:
            result = await mcp.call_tool("echo", {"text": "x", "baton_intent": "vendor-value"})
            await handle.flush()
        finally:
            await handle.aclose()

        # forwarded to the vendor handler (not stripped)
        assert "vendor-value" in json.dumps(result, default=str)
        # and never captured as call_intent (disposition = native)
        events = _read_events(events_path)
        start = next(e for e in events if e["event_type"] == "tool_call_start")
        assert start["payload"].get("call_intent") is None
        # native param means no synthesised proactive either
        assert not any(e["event_type"] == "annotation" for e in events)


# =============================================================================
# tools/call strip + capture
# =============================================================================


class TestCallStripAndCapture:
    async def test_strips_intent_and_captures_call_intent(self, events_path: str) -> None:
        seen: dict[str, Any] = {}
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def echo(text: str) -> str:
            # If baton_intent leaked to the handler, mcp would reject the call
            # (unexpected kwarg) — reaching here proves it was stripped.
            seen["text"] = text
            return text

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:
            await mcp.call_tool(
                "echo", {"text": "hello", INTENT_PARAM_NAME: "user wants a greeting"}
            )
            await handle.flush()
        finally:
            await handle.aclose()

        assert seen == {"text": "hello"}  # vendor handler never saw baton_intent
        events = _read_events(events_path)
        start = next(e for e in events if e["event_type"] == "tool_call_start")
        assert start["payload"]["call_intent"] == "user wants a greeting"
        assert start["payload"]["intent_source"] == INTENT_SOURCE_PARAM
        # params captured == vendor-visible args, no baton_intent
        assert start["payload"]["params"] == {"text": "hello"}

    async def test_no_intent_leaves_call_intent_null(self, events_path: str) -> None:
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:
            await mcp.call_tool("echo", {"text": "hi"})
            await handle.flush()
        finally:
            await handle.aclose()

        events = _read_events(events_path)
        start = next(e for e in events if e["event_type"] == "tool_call_start")
        assert start["payload"].get("call_intent") is None
        assert start["payload"].get("intent_source") is None
        assert not any(e["event_type"] == "annotation" for e in events)


# =============================================================================
# proactive synthesis + dedup
# =============================================================================


class TestProactiveSynthesis:
    async def test_first_intent_emits_proactive_before_start(self, events_path: str) -> None:
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:
            await mcp.call_tool("echo", {"text": "x", INTENT_PARAM_NAME: "why the user called"})
            await handle.flush()
        finally:
            await handle.aclose()

        events = _read_events(events_path)
        assert events[0]["event_type"] == "annotation", "proactive must be sequenced first"
        ann = events[0]
        assert ann["payload"]["intent"] == "why the user called"
        assert ann["payload"]["intent_source"] == INTENT_SOURCE_PARAM
        assert ann["payload"]["tool_name"] == "echo"
        start = next(e for e in events if e["event_type"] == "tool_call_start")
        assert ann["sequence_number"] < start["sequence_number"]

    async def test_only_one_proactive_per_session(self, events_path: str) -> None:
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:
            await mcp.call_tool("echo", {"text": "1", INTENT_PARAM_NAME: "first why"})
            await mcp.call_tool("echo", {"text": "2", INTENT_PARAM_NAME: "second why"})
            await handle.flush()
        finally:
            await handle.aclose()

        events = _read_events(events_path)
        annotations = [e for e in events if e["event_type"] == "annotation"]
        assert len(annotations) == 1, "only the session's first intent synthesises a proactive"
        # but the second call still rides its intent on the start event
        starts = [e for e in events if e["event_type"] == "tool_call_start"]
        assert starts[1]["payload"]["call_intent"] == "second why"

    async def test_real_proactive_annotation_suppresses_synthesised(self, events_path: str) -> None:
        """A real proactive annotation-tool call claims the session's slot via the
        shared tracker, so a later injected-param intent does NOT synthesise a
        second proactive."""
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:
            await mcp.call_tool("test-vendor_annotate", {"intent": "real proactive"})
            await mcp.call_tool("echo", {"text": "x", INTENT_PARAM_NAME: "injected why"})
            await handle.flush()
        finally:
            await handle.aclose()

        events = _read_events(events_path)
        annotations = [e for e in events if e["event_type"] == "annotation"]
        assert len(annotations) == 1
        assert annotations[0]["payload"]["intent"] == "real proactive"
        # the injected intent still rides the start event
        start = next(e for e in events if e["event_type"] == "tool_call_start")
        assert start["payload"]["call_intent"] == "injected why"
