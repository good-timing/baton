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

from baton.integrations._llm_text import build_user_goal_param_description
from baton.integrations.fastmcp import VendorConfig, install_baton
from baton.sinks import HttpSink
from tests._event_helpers import without_surface_snapshots


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
            # These suites exercise the agent-initiated proactive path,
            # which proactive_mode="off" (the default) now rejects.
            proactive_mode="on",
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

    async def test_annotation_tool_requires_the_goal(self, httpserver: HTTPServer) -> None:
        """``intent`` is the one load-bearing field on every annotation —
        proactives describe what's being attempted, reactives describe
        what the failed attempt was attempting. Without it the annotation
        is a payloadless event. Mirrors baton-proxy 0.1.3 (proxy.py:93)
        which made a single explicit required schema property
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
            assert "user_goal" in required, (
                f"user_goal must be required on annotation tool schema; required={required}"
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

    async def test_both_capture_channels_off_is_rejected(self, httpserver: HTTPServer) -> None:
        """intent_param_mode='off' + proactive_mode='off' captures no intent at
        all — a silent no-op install. Fail loudly at construction instead."""
        mcp = FastMCP("x")
        with pytest.raises(ValueError, match="nothing would capture"):
            install_baton(
                mcp,
                VendorConfig(
                    vendor_id="v",
                    vendor_display_name="V",
                    consent_token="ct_test",
                    sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
                    intent_param_mode="off",
                    proactive_mode="off",
                ),
            )

    async def test_proactive_mode_must_be_valid(self, httpserver: HTTPServer) -> None:
        mcp = FastMCP("x")
        with pytest.raises(ValueError, match="proactive_mode"):
            install_baton(
                mcp,
                VendorConfig(
                    vendor_id="v",
                    vendor_display_name="V",
                    consent_token="ct_test",
                    sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
                    proactive_mode="disabled",
                ),
            )

    async def test_annotation_tool_survives_proactive_off(self, httpserver: HTTPServer) -> None:
        """The reactive channel is the product. Default (proactive off) must
        still expose the annotation tool and still accept a reactive call."""
        httpserver.expect_request("/v0/events").respond_with_response(Response(status=202))
        mcp = FastMCP("x")
        install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                consent_token="ct_test",
                sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
            ),
        )
        async with Client(mcp) as client:
            names = [t.name for t in await client.list_tools()]
            assert "v_annotate" in names
            await client.call_tool(
                "v_annotate",
                {
                    "user_goal": "find the thing",
                    "signal_type": "failure",
                    "suggested_improvement": "return a typed error",
                },
            )

    async def test_proactive_annotation_is_rejected_by_default(
        self, httpserver: HTTPServer
    ) -> None:
        """proactive_mode="off" enforces at the handler, not just in the prompt.
        A stray proactive annotation carries an umbrella `overall_task` label that
        would outrank the per-call one in any consumer keying grouping on it,
        so the call must not produce an event at all."""
        captured: list[dict[str, Any]] = []

        def ingest(request: Any) -> Response:
            captured.append(request.get_json())
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(ingest)
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
            async with Client(mcp) as client:
                result = await client.call_tool("v_annotate", {"user_goal": "about to look"})
            await handle.flush()
        finally:
            await handle.aclose()

        assert "reactive-only" in str(result.content)
        annotations = [
            ev
            for batch in captured
            for ev in (batch if isinstance(batch, list) else [batch])
            if ev.get("event_type") == "annotation"
        ]
        assert annotations == [], "a rejected proactive must not reach the sink"

    async def test_reactive_annotation_still_lands_by_default(self, httpserver: HTTPServer) -> None:
        """The mirror of the above — rejecting proactives must not cost us the
        friction signal, which is the product."""
        captured: list[dict[str, Any]] = []

        def ingest(request: Any) -> Response:
            captured.append(request.get_json())
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(ingest)
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
            async with Client(mcp) as client:
                await client.call_tool(
                    "v_annotate",
                    {
                        "user_goal": "find the thing",
                        "signal_type": "feature_gap",
                        "suggested_improvement": "add a remove_item tool",
                    },
                )
            await handle.flush()
        finally:
            await handle.aclose()

        annotations = [
            ev
            for batch in captured
            for ev in (batch if isinstance(batch, list) else [batch])
            if ev.get("event_type") == "annotation"
        ]
        assert len(annotations) == 1
        assert annotations[0]["payload"]["signal_type"] == "feature_gap"


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
                    "user_goal": "summarize PR comments",
                    "expected_result": "2-3 sentence paragraph",
                    "overall_task": "code-review",
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
        # Agent sends `overall_task`; the wire carries `workflow`.
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
                    "user_goal": "fetch the search results",
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
            await client.call_tool("test-vendor_annotate", {"user_goal": "x"})

        await handle.flush()
        types = [ev["event_type"] for ev in captured]
        # Exactly one annotation event, no tool_call_start/end pair
        assert types.count("annotation") == 1
        assert "tool_call_start" not in types
        assert "tool_call_end" not in types


class TestResolveSessionIdHookOnAnnotationTool:
    """The fastmcp adapter's annotation tool also checks rung 0 (item 3,
    sdk-hardening thread), so an explicit reactive/proactive annotation call
    stitches to the same hook-resolved session id as the tool calls around
    it. (The mcp adapter's annotation tool has a pre-existing, documented
    limitation here — see ``mcp/annotation.py``.)"""

    async def test_hook_wins_for_explicit_annotation_call(
        self, httpserver: HTTPServer, captured: list[dict[str, Any]]
    ) -> None:
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
                resolve_session_id=lambda ctx: "vendor-resolved",
            ),
        )

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})
            await client.call_tool(
                "test-vendor_annotate",
                {"user_goal": "explain this", "signal_type": "dead_end"},
            )

        await handle.flush()
        await handle.aclose()

        # surface_snapshot is a process-level event (proxy parity — see
        # integrations._surface) and intentionally does NOT go through the
        # per-call resolve_session_id hook; every other event type must.
        non_surface = [ev for ev in captured if ev["event_type"] != "surface_snapshot"]
        assert non_surface
        session_ids = {ev["session_id"] for ev in non_surface}
        assert session_ids == {"vendor-resolved"}

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
                {"user_goal": "x"},
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
                {"user_goal": "find user"},
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
        for ev in without_surface_snapshots(captured):
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
                {"user_goal": "find user", "expected_result": "user record"},
            )
            # 2. Real tool call
            await client.call_tool("lookup", {"name": "alice"})
            # 3. Reactive annotate — intent restated so the reactive
            # carries the same task framing the proactive opened.
            await client.call_tool(
                "test-vendor_annotate",
                {
                    "user_goal": "find user",
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
            await client.call_tool("test-vendor_annotate", {"user_goal": "x"})
            await client.call_tool("echo", {"text": "y"})

        await handle.flush()
        # surface_snapshot sits on its own (fallback) session/counter — see
        # tests._event_helpers — excluded here, which is about the
        # annotation-tool and middleware paths sharing ONE counter.
        seqs = [ev["sequence_number"] for ev in without_surface_snapshots(captured)]
        assert seqs == sorted(seqs), (
            "sequence numbers must be monotonic across annotation + tool_call events"
        )
        assert len(set(seqs)) == len(seqs), "sequence numbers must be unique"


# =============================================================================
# surface_snapshot — item 2, sdk-hardening thread (see integrations._surface)
# =============================================================================


class TestSurfaceSnapshot:
    async def test_emits_on_first_tools_list_excludes_annotation_tool(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.list_tools()

        await handle.flush()
        snapshots = [ev for ev in captured if ev["event_type"] == "surface_snapshot"]
        assert len(snapshots) == 1
        payload = snapshots[0]["payload"]
        assert [t["name"] for t in payload["tools"]] == ["echo"]
        assert payload["seam_augmentations"]["injected_tools"] == ["test-vendor_annotate"]
        assert payload["surface_hash"].startswith("sha256:")

    async def test_dedupes_across_repeated_tools_list(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.list_tools()
            await client.list_tools()
            await client.call_tool("echo", {"text": "x"})

        await handle.flush()
        snapshots = [ev for ev in captured if ev["event_type"] == "surface_snapshot"]
        assert len(snapshots) == 1

    async def test_tools_are_vendor_true_pre_injection(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        """The surface snapshot's ``tools`` must reflect the vendor's real
        schema, not Baton's injected ``user_goal``/``expected_result`` — the
        hash is the identity change specs are authored against and must not
        drift when e.g. ``intent_param_mode`` is toggled."""
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.list_tools()
            # The AGENT-visible schema, post-injection — proves the snapshot
            # (asserted below) differs from what's actually advertised.
            advertised = await client.list_tools()

        await handle.flush()
        advertised_echo = next(t for t in advertised if t.name == "echo")
        assert "user_goal" in (advertised_echo.inputSchema.get("properties") or {})

        snapshot = next(ev for ev in captured if ev["event_type"] == "surface_snapshot")["payload"]
        echo_tool = next(t for t in snapshot["tools"] if t["name"] == "echo")
        assert "user_goal" not in echo_tool["inputSchema"].get("properties", {})
        assert "expected_result" not in echo_tool["inputSchema"].get("properties", {})

    async def test_instructions_are_vendor_true(
        self,
        configured_mcp: tuple[FastMCP, Any],
        captured: list[dict[str, Any]],
    ) -> None:
        """``configured_mcp`` never sets vendor instructions before
        ``install_baton`` — the snapshot must capture that (``None``), not
        Baton's own suffixed ``mcp.instructions``."""
        mcp, handle = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.list_tools()

        await handle.flush()
        assert mcp.instructions is not None  # Baton did set something
        snapshot = next(ev for ev in captured if ev["event_type"] == "surface_snapshot")["payload"]
        assert snapshot["instructions"] is None


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

        # The annotation tool declares `user_goal` itself, so its presence
        # proves nothing; what proves injection skipped the tool is that its
        # schema does not carry the INJECTED description, which only the
        # injector writes.
        annotate_tool = next(t for t in tools if t.name == "test-vendor_annotate")
        annotate_props = annotate_tool.inputSchema.get("properties", {})
        assert "user_goal" in annotate_props, "the tool declares it natively"
        assert build_user_goal_param_description() not in str(annotate_props)
        # Both labels, so the pin cannot go vacuous under `required`.
        assert build_user_goal_param_description(intent_param_mode="required") not in str(
            annotate_props
        )
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
            await client.call_tool("test-vendor_annotate", {"user_goal": "real proactive intent"})
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
