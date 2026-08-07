"""End-to-end test for install_baton() per SPEC §11 + CHARTER ADR-4.

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

from baton.integrations.fastmcp import VendorConfig, install_baton
from baton.sinks import HttpSink


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
            consent_token="ct_test",
            sink=HttpSink(url=httpserver.url_for(""), api_key="test-api-key"),
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
                consent_token="ct_test",
                sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
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
                consent_token="ct_test",
                sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
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
                consent_token="ct_test",
                sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
            ),
        )
        try:
            tools = {t.name for t in await mcp.list_tools()}
            assert "v_annotate" in tools
        finally:
            await handle.aclose()

    async def test_annotation_tool_requires_intent(self, httpserver: HTTPServer) -> None:
        """``intent`` is the one load-bearing field on every annotation —
        proactives describe what's being attempted, reactives describe
        what the failed attempt was attempting. Without it the annotation
        is a payloadless event. Mirrors baton-proxy 0.1.3 (proxy.py:93)
        which made ``required: ["intent"]`` an explicit schema property
        instead of relying on Python's None default to leave it optional."""
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                consent_token="ct_test",
                sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
            ),
        )
        try:
            tools = await mcp.list_tools()
            # FastMCP exposes a FunctionTool wrapper; convert to the
            # MCP-native shape so the schema check matches the mcp adapter.
            annotate = next(t for t in tools if t.name == "v_annotate").to_mcp_tool()
            required = annotate.inputSchema.get("required", [])
            assert "intent" in required, (
                f"intent must be required on annotation tool schema; required={required}"
            )
            # signal_type + suggested_improvement stay optional — the
            # tool description marks them reactive-only and they're
            # absent on every proactive annotation.
            assert "signal_type" not in required
            assert "suggested_improvement" not in required
        finally:
            await handle.aclose()

    async def test_annotation_tool_name_override(self, httpserver: HTTPServer) -> None:
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                consent_token="ct_test",
                sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
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
                    consent_token="ct_test",
                    sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
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
                    "intent": "fetch the search results",
                    "signal_type": "dead_end",
                    "suggested_improvement": "surface clearer error",
                    "context": {"likely_cause": "content_filter"},
                },
            )

        await handle.flush()
        annotation_events = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(annotation_events) == 1
        payload = annotation_events[0]["payload"]
        assert payload["intent"] == "fetch the search results"
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
# Runtime-meta capture (SPEC §11.4 — used by worker for cycle correlation)
# =============================================================================


class TestRuntimeMetaCapture:
    async def test_tool_call_captures_runtime_meta_when_client_provides_it(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        """``runtime_meta`` field on tool-call events MUST contain the
        client-supplied ``_meta`` dict (verbatim, or scrubbed). This is what
        lets the worker derive per-turn correlation downstream of the SDK."""
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo",
                {"text": "hi"},
                meta={
                    "claudecode/toolUseId": "tool-use-xyz",
                    "claudecode/sessionId": "sess-abc",
                },
            )

        await handle.flush()
        starts = [ev for ev in captured if ev["event_type"] == "tool_call_start"]
        ends = [ev for ev in captured if ev["event_type"] == "tool_call_end"]
        assert len(starts) == 1 and len(ends) == 1

        for ev in (starts[0], ends[0]):
            assert ev["runtime_meta"] is not None
            assert ev["runtime_meta"].get("claudecode/toolUseId") == "tool-use-xyz"
            assert ev["runtime_meta"].get("claudecode/sessionId") == "sess-abc"

    async def test_annotation_captures_runtime_meta(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        """Annotation events MUST carry ``runtime_meta`` for the same
        correlation reason — proactive annotations are turn boundaries; the
        worker needs the runtime ID alongside them."""
        mcp, handle = configured_mcp

        async with Client(mcp) as client:
            await client.call_tool(
                "test-vendor_annotate",
                {"intent": "find user"},
                meta={"claudecode/toolUseId": "tool-use-1"},
            )

        await handle.flush()
        annots = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(annots) == 1
        assert annots[0]["runtime_meta"] is not None
        assert annots[0]["runtime_meta"].get("claudecode/toolUseId") == "tool-use-1"

    async def test_runtime_meta_captures_wire_level_progresstoken_only(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        """When the caller doesn't supply ``_meta`` explicitly, FastMCP still
        injects ``progressToken`` at the wire level. ``runtime_meta`` should
        capture exactly that — proves we're faithful to what crossed the
        transport, not what the caller intended."""
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "hi"})

        await handle.flush()
        for ev in captured:
            # progressToken is always present on the wire even without
            # caller-supplied meta — captures the MCP runtime's own
            # bookkeeping. Vendor-supplied runtime IDs would also land here.
            assert ev["runtime_meta"] is not None
            assert "progressToken" in ev["runtime_meta"]


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
            # 3. Reactive annotate — intent restated so the reactive
            # carries the same task framing the proactive opened.
            await client.call_tool(
                "test-vendor_annotate",
                {
                    "intent": "find user",
                    "signal_type": "dead_end",
                    "suggested_improvement": "...",
                },
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


# =============================================================================
# handle.escalate()
# =============================================================================


class TestEscalate:
    async def test_escalate_calls_console_and_returns_ticket(
        self,
        httpserver: HTTPServer,
    ) -> None:
        """escalate() POSTs to /v0/escalate and surfaces ticket_id + ticket_url."""
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        httpserver.expect_request("/v0/escalate", method="POST").respond_with_data(
            '{"ticket_id": "1042", "ticket_url": "https://example.com/issues/1042"}',
            content_type="application/json",
            status=201,
        )

        mcp = FastMCP("test-vendor")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=HttpSink(url=httpserver.url_for(""), api_key="test-key"),
            ),
        )

        result = await handle.escalate(annotation_seq=3)
        await handle.aclose()

        assert result["ticket_id"] == "1042"
        assert result["ticket_url"] == "https://example.com/issues/1042"

        # Verify the request shape sent to Console.
        escalate_requests = [r for r in httpserver.log if r[0].path == "/v0/escalate"]
        assert len(escalate_requests) == 1
        body = escalate_requests[0][0].get_json()
        assert body["session_id"] == handle.session_id  # fallback when no ctx session_id
        assert body["annotation_seq"] == 3

    async def test_escalate_explicit_session_id_overrides_handle(
        self,
        httpserver: HTTPServer,
    ) -> None:
        """session_id kwarg overrides handle.session_id — used when fastmcp ctx
        provides a runtime MCP session UUID different from the SDK fallback."""
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        httpserver.expect_request("/v0/escalate", method="POST").respond_with_data(
            '{"ticket_id": "9999", "ticket_url": "https://example.com/issues/9999"}',
            content_type="application/json",
            status=201,
        )

        mcp = FastMCP("test-vendor")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=HttpSink(url=httpserver.url_for(""), api_key="test-key"),
            ),
        )

        runtime_session_id = "mcp-runtime-session-abc123"
        assert runtime_session_id != handle.session_id  # confirm they differ

        result = await handle.escalate(session_id=runtime_session_id)
        await handle.aclose()

        assert result["ticket_id"] == "9999"
        escalate_requests = [r for r in httpserver.log if r[0].path == "/v0/escalate"]
        body = escalate_requests[0][0].get_json()
        assert body["session_id"] == runtime_session_id  # runtime ID used, not fallback

    async def test_escalate_dev_mode_returns_queued(self) -> None:
        """escalate() returns a dev-mode sentinel when sink has no Console URL."""
        from baton.sinks import StdoutSink

        mcp = FastMCP("test-vendor")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=StdoutSink(),
            ),
        )

        result = await handle.escalate()
        await handle.aclose()

        assert result["ticket_id"] == "queued"
        assert result["ticket_url"] is None


# =============================================================================
# Intent-param injection wired end-to-end via install_baton
# =============================================================================


class TestIntentInjectionInstalled:
    async def test_param_injected_and_captured_through_install(
        self, configured_mcp: tuple[FastMCP, Any], captured: list[dict[str, Any]]
    ) -> None:
        """install_baton wires injection on by default; a call carrying
        user_goal captures call_intent and synthesises a proactive."""
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            echo_tool = next(t for t in tools if t.name == "echo")
            await client.call_tool("echo", {"text": "x", "user_goal": "the why"})

        await handle.flush()

        # annotation tool is NOT injected with user_goal
        annotate_tool = next(t for t in tools if t.name == "test-vendor_annotate")
        assert "user_goal" not in annotate_tool.inputSchema.get("properties", {})
        assert "user_goal" in echo_tool.inputSchema["properties"]
        assert "test-vendor_annotate" in names

        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"]["call_intent"] == "the why"
        anns = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(anns) == 1
        assert anns[0]["payload"]["intent_source"] == "injected_param"

    async def test_real_proactive_suppresses_synthesised_one(
        self, configured_mcp: tuple[FastMCP, Any], captured: list[dict[str, Any]]
    ) -> None:
        """When the agent calls the annotation tool proactively first, the
        middleware must NOT also synthesise a proactive from the injected param
        (shared ProactiveTracker) — but the tool call still rides its intent."""
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            # Agent's real proactive annotation (no signal_type) fires first.
            await client.call_tool("test-vendor_annotate", {"intent": "real proactive intent"})
            # Then the wrapped tool call carrying an injected intent.
            await client.call_tool("echo", {"text": "x", "user_goal": "param intent"})

        await handle.flush()

        anns = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(anns) == 1, "the real proactive suppresses the synthesised one"
        assert anns[0]["payload"]["intent"] == "real proactive intent"
        assert anns[0]["payload"].get("intent_source") is None  # real, not injected
        # the param intent is not lost — it still rides tool_call_start
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"]["call_intent"] == "param intent"
