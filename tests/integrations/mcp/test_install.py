"""End-to-end test for install_baton() per SPEC §11 + CHARTER OD-7.

Spec-first, failing-test-first: written BEFORE ``src/baton/install.py``.

Strategy: build a real FastMCP server, install Baton on it, register a vendor
tool, drive interactions through FastMCP's in-process Client (real tool calls
+ real annotation calls), and verify all four event types land at the
pytest-httpserver fake ingest endpoint.

This is the integration test that proves the five-line vendor integration
shape works end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.integrations.mcp import VendorConfig, install_baton


@pytest.fixture
async def captured() -> list[dict[str, Any]]:
    return []


@pytest.fixture
async def configured_mcp(
    httpserver: HTTPServer,
    captured: list[dict[str, Any]],
) -> tuple[FastMCP, Any]:
    """Provide a FastMCP server with install_baton applied. Yields (mcp, handle).
    The handle is what install_baton returns — used for explicit flush/aclose
    in tests."""

    def ingest_handler(request: Any) -> Response:
        captured.append(request.get_json())
        return Response("", status=201)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(ingest_handler)

    mcp = FastMCP("test-vendor-mcp")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-vendor",
            vendor_display_name="Test Vendor",
            console_url=httpserver.url_for(""),
            api_key="test-api-key",
            consent_token="ct_test",
        ),
    )

    yield mcp, handle
    await handle.aclose()


# =============================================================================
# Five-line integration shape
# =============================================================================


class TestInstallation:
    async def test_returns_handle_with_flush_and_aclose(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                console_url=httpserver.url_for(""),
                api_key="k",
                consent_token="ct_test",
            ),
        )
        # Handle exposes flush + aclose
        await handle.flush()
        await handle.aclose()

    async def test_sets_server_instructions(self, httpserver: HTTPServer) -> None:
        """install_baton sets MCP server instructions templated from vendor name."""
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="acme",
                vendor_display_name="ACME Corp",
                console_url=httpserver.url_for(""),
                api_key="k",
                consent_token="ct_test",
            ),
        )
        try:
            # Server instructions should be set and mention the vendor display name
            assert mcp.instructions is not None
            assert "ACME Corp" in mcp.instructions
            # And reference the annotation tool name (default: vendor_id + _annotate)
            assert "acme_annotate" in mcp.instructions
        finally:
            await handle.aclose()

    async def test_annotation_tool_registered(self, httpserver: HTTPServer) -> None:
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                console_url=httpserver.url_for(""),
                api_key="k",
                consent_token="ct_test",
            ),
        )
        try:
            tools = {t.name for t in await mcp.list_tools()}
            assert "v_annotate" in tools
        finally:
            await handle.aclose()

    async def test_annotation_tool_name_override(self, httpserver: HTTPServer) -> None:
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                console_url=httpserver.url_for(""),
                api_key="k",
                consent_token="ct_test",
                annotation_tool_name="custom-annotate-name",
            ),
        )
        try:
            tools = {t.name for t in await mcp.list_tools()}
            assert "custom-annotate-name" in tools
            assert "v_annotate" not in tools
        finally:
            await handle.aclose()

    async def test_vendor_id_must_be_valid(self, httpserver: HTTPServer) -> None:
        mcp = FastMCP("x")
        # Dot in vendor_id would produce dotted annotation tool name; Claude Desktop rejects.
        with pytest.raises(ValueError, match="vendor_id"):
            install_baton(
                mcp,
                VendorConfig(
                    vendor_id="bad.id",
                    vendor_display_name="V",
                    console_url=httpserver.url_for(""),
                    api_key="k",
                ),
            )


# =============================================================================
# End-to-end: tool call → tool_call_start + tool_call_end events
# =============================================================================


class TestEndToEndToolCall:
    async def test_real_tool_call_emits_start_and_end(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "hello"})

        await handle.flush()
        types = [ev["event_type"] for ev in captured]
        assert "tool_call_start" in types
        assert "tool_call_end" in types

    async def test_failed_tool_emits_start_and_error(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        mcp, handle = configured_mcp

        @mcp.tool()
        def boom() -> None:
            raise RuntimeError("boom")

        async with Client(mcp) as client:
            with pytest.raises(Exception):  # noqa: B017 fastmcp wraps; we only care that something raised
                await client.call_tool("boom", {})

        await handle.flush()
        types = [ev["event_type"] for ev in captured]
        assert "tool_call_start" in types
        assert "tool_call_error" in types


# =============================================================================
# End-to-end: annotation tool → annotation event
# =============================================================================


class TestAnnotationToolEndToEnd:
    async def test_proactive_annotation_emits_annotation_event(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        mcp, handle = configured_mcp

        async with Client(mcp) as client:
            await client.call_tool(
                "test-vendor_annotate",
                {
                    "intent": "summarize PR comments",
                    "expected_outcome": "2-3 sentence paragraph",
                    "workflow": "code-review",
                },
            )

        await handle.flush()
        # Only annotation events emit; the middleware skips tool_call_*
        # for the annotation tool itself.
        annotation_events = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(annotation_events) == 1
        payload = annotation_events[0]["payload"]
        assert payload["intent"] == "summarize PR comments"
        assert payload["expected_outcome"] == "2-3 sentence paragraph"
        assert payload["workflow"] == "code-review"
        assert payload["signal_type"] is None

    async def test_reactive_annotation_with_signal_type(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        mcp, handle = configured_mcp

        async with Client(mcp) as client:
            await client.call_tool(
                "test-vendor_annotate",
                {
                    "signal_type": "dead_end",
                    "suggested_improvement": "surface clearer error",
                    "context": {"likely_cause": "content_filter"},
                },
            )

        await handle.flush()
        annotation_events = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(annotation_events) == 1
        payload = annotation_events[0]["payload"]
        assert payload["signal_type"] == "dead_end"
        assert payload["suggested_improvement"] == "surface clearer error"
        assert payload["context"]["likely_cause"] == "content_filter"

    async def test_annotation_does_not_emit_tool_call_events(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        """Middleware MUST skip tool_call_start/end emit when the agent
        calls the annotation tool — the annotation handler emits its own
        annotation event, not a wrapping tool-call pair."""
        mcp, handle = configured_mcp

        async with Client(mcp) as client:
            await client.call_tool("test-vendor_annotate", {"intent": "x"})

        await handle.flush()
        types = [ev["event_type"] for ev in captured]
        # Exactly one annotation event, no tool_call_start/end pair
        assert types.count("annotation") == 1
        assert "tool_call_start" not in types
        assert "tool_call_end" not in types

    async def test_annotation_detects_claude_code_from_meta(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        """Annotation events MUST honor the same runtime detection as the
        middleware path — reading ``_meta.claudecode/toolUseId`` from the
        request context."""
        mcp, handle = configured_mcp

        async with Client(mcp) as client:
            await client.call_tool(
                "test-vendor_annotate",
                {"intent": "x"},
                meta={"claudecode/toolUseId": "tool-use-xyz"},
            )

        await handle.flush()
        annotation_events = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(annotation_events) == 1
        assert annotation_events[0]["agent_runtime"] == "claude-code"


# =============================================================================
# All four event types in one flow (the full thesis-end-to-end test)
# =============================================================================


class TestAllFourEventTypesInOneFlow:
    async def test_proactive_annotate_then_tool_call_then_reactive_annotate(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        """The canonical agent flow: announce intent, call tool, react to result.
        Worker stitches these into a SignalPayload per SPEC §11.5."""
        mcp, handle = configured_mcp

        @mcp.tool()
        def lookup(name: str) -> str:
            return f"found: {name}"

        async with Client(mcp) as client:
            # 1. Proactive annotate
            await client.call_tool(
                "test-vendor_annotate",
                {"intent": "find user", "expected_outcome": "user record"},
            )
            # 2. Real tool call
            await client.call_tool("lookup", {"name": "alice"})
            # 3. Reactive annotate
            await client.call_tool(
                "test-vendor_annotate",
                {"signal_type": "dead_end", "suggested_improvement": "..."},
            )

        await handle.flush()
        types = [ev["event_type"] for ev in captured]
        assert types.count("annotation") == 2  # proactive + reactive
        assert types.count("tool_call_start") == 1
        assert types.count("tool_call_end") == 1

    async def test_sequence_numbers_monotonic_across_event_types(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        """The annotation tool and the middleware must share a sequence-number
        counter so sequence numbers stay monotonic within a session regardless
        of which path emitted the event."""
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("test-vendor_annotate", {"intent": "x"})
            await client.call_tool("echo", {"text": "y"})

        await handle.flush()
        seqs = [ev["sequence_number"] for ev in captured]
        assert seqs == sorted(seqs), (
            "sequence numbers must be monotonic across annotation + tool_call events"
        )
        assert len(set(seqs)) == len(seqs), "sequence numbers must be unique"
