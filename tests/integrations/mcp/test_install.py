"""End-to-end tests for the official-mcp-SDK adapter (``baton.integrations.mcp``).

Mirrors ``tests/integrations/fastmcp/test_install.py`` but targets the official
``mcp.server.fastmcp.FastMCP`` and drives tools via ``mcp.call_tool(name, args)``
directly (the official SDK doesn't ship an in-process client equivalent to
fastmcp's ``Client``; the wrap layer's emission semantics are independent of
transport).

Sink: ``FileSink`` → temp JSONL → read back for assertions. Tests the
integration's emission shape end-to-end; sink reliability + HTTP delivery are
covered by ``tests/test_sinks.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from baton.integrations.mcp import VendorConfig, install_baton
from baton.sinks import FileSink


@pytest.fixture
def events_path(tmp_path: Any) -> str:
    return str(tmp_path / "events.jsonl")


@pytest.fixture
async def configured_mcp(events_path: str) -> Any:
    """Provide a FastMCP server with install_baton applied. Yields (mcp, handle, events_path)."""
    mcp = FastMCP("test-vendor-mcp")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-vendor",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(events_path),
        ),
    )
    yield mcp, handle, events_path
    await handle.aclose()


def _read_events(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# =============================================================================
# Install-time validation + wiring
# =============================================================================


class TestInstallation:
    async def test_returns_handle_with_flush_and_aclose(self, events_path: str) -> None:
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        await handle.flush()
        await handle.aclose()

    async def test_sets_server_instructions(self, events_path: str) -> None:
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="acme",
                vendor_display_name="ACME Corp",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:
            assert mcp.instructions is not None
            assert "ACME Corp" in mcp.instructions
            assert "acme_annotate" in mcp.instructions
        finally:
            await handle.aclose()

    async def test_annotation_tool_registered(self, events_path: str) -> None:
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:
            tools = {t.name for t in await mcp.list_tools()}
            assert "v_annotate" in tools
        finally:
            await handle.aclose()

    async def test_annotation_tool_requires_intent(self, events_path: str) -> None:
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
                sink=FileSink(events_path),
            ),
        )
        try:
            tools = await mcp.list_tools()
            annotate = next(t for t in tools if t.name == "v_annotate")
            required = annotate.inputSchema.get("required", [])
            assert "intent" in required, (
                f"intent must be required on annotation tool schema; "
                f"required={required}"
            )
            # signal_type + suggested_improvement stay optional — the
            # tool description marks them reactive-only and they're
            # absent on every proactive annotation.
            assert "signal_type" not in required
            assert "suggested_improvement" not in required
        finally:
            await handle.aclose()

    async def test_annotation_tool_name_override(self, events_path: str) -> None:
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                consent_token="ct_test",
                sink=FileSink(events_path),
                annotation_tool_name="custom-annotate-name",
            ),
        )
        try:
            tools = {t.name for t in await mcp.list_tools()}
            assert "custom-annotate-name" in tools
            assert "v_annotate" not in tools
        finally:
            await handle.aclose()

    async def test_vendor_id_must_be_valid(self, events_path: str) -> None:
        mcp = FastMCP("x")
        with pytest.raises(ValueError, match="vendor_id"):
            install_baton(
                mcp,
                VendorConfig(
                    vendor_id="bad.id",
                    vendor_display_name="V",
                    consent_token="ct_test",
                    sink=FileSink(events_path),
                ),
            )

    async def test_consent_token_required(self, events_path: str) -> None:
        mcp = FastMCP("x")
        with pytest.raises(ValueError, match="consent_token"):
            install_baton(
                mcp,
                VendorConfig(
                    vendor_id="v",
                    vendor_display_name="V",
                    consent_token="",
                    sink=FileSink(events_path),
                ),
            )

    async def test_post_install_tool_gets_wrapped(self, events_path: str) -> None:
        """Tools registered AFTER install_baton must be auto-wrapped via the
        patched add_tool. Verifies the post-install registration path."""
        mcp = FastMCP("x")
        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                consent_token="ct_test",
                sink=FileSink(events_path),
            ),
        )
        try:

            @mcp.tool()
            def post(x: int) -> int:
                return x * 2

            await mcp.call_tool("post", {"x": 5})
            await handle.flush()
        finally:
            await handle.aclose()

        events = _read_events(events_path)
        types = [e["event_type"] for e in events]
        assert "tool_call_start" in types
        assert "tool_call_end" in types
        start = next(e for e in events if e["event_type"] == "tool_call_start")
        assert start["payload"]["params"] == {"x": 5}


# =============================================================================
# Tool-call emission: sync + async + error
# =============================================================================


class TestEndToEndToolCall:
    async def test_sync_tool_emits_start_and_end_with_params(
        self, configured_mcp: tuple[Any, Any, str]
    ) -> None:
        mcp, handle, path = configured_mcp

        @mcp.tool()
        def add(a: int, b: int) -> int:
            return a + b

        await mcp.call_tool("add", {"a": 2, "b": 3})
        await handle.flush()

        events = _read_events(path)
        types = [e["event_type"] for e in events]
        assert types == ["tool_call_start", "tool_call_end"]

        start = events[0]["payload"]
        assert start["tool_name"] == "add"
        assert start["params"] == {"a": 2, "b": 3}

        end = events[1]["payload"]
        assert end["tool_name"] == "add"
        assert end["result"] == 5
        assert end["duration_ms"] >= 0

    async def test_async_tool_emits_start_and_end_with_params(
        self, configured_mcp: tuple[Any, Any, str]
    ) -> None:
        import asyncio as _asyncio

        mcp, handle, path = configured_mcp

        @mcp.tool()
        async def echo(msg: str, repeat: int = 1) -> str:
            await _asyncio.sleep(0)
            return msg * repeat

        await mcp.call_tool("echo", {"msg": "hi", "repeat": 2})
        await handle.flush()

        events = _read_events(path)
        types = [e["event_type"] for e in events]
        assert types == ["tool_call_start", "tool_call_end"]

        start = events[0]["payload"]
        assert start["params"] == {"msg": "hi", "repeat": 2}
        end = events[1]["payload"]
        assert end["result"] == "hihi"

    async def test_failed_tool_emits_start_and_error(
        self, configured_mcp: tuple[Any, Any, str]
    ) -> None:
        mcp, handle, path = configured_mcp

        @mcp.tool()
        def boom() -> str:
            raise RuntimeError("intentional")

        with pytest.raises(Exception):  # noqa: B017  mcp wraps in ToolError; we only verify it raises
            await mcp.call_tool("boom", {})

        await handle.flush()

        events = _read_events(path)
        types = [e["event_type"] for e in events]
        assert types == ["tool_call_start", "tool_call_error"]

        err = events[1]["payload"]
        assert err["tool_name"] == "boom"
        assert err["error_type"] == "RuntimeError"
        assert "intentional" in err["error_body"]


# =============================================================================
# Annotation tool emission
# =============================================================================


class TestAnnotationToolEndToEnd:
    async def test_proactive_annotation_emits_annotation_event(
        self, configured_mcp: tuple[Any, Any, str]
    ) -> None:
        mcp, handle, path = configured_mcp
        await mcp.call_tool(
            "test-vendor_annotate",
            {
                "intent": "summarize PR comments",
                "expected_outcome": "2-3 sentence paragraph",
                "workflow": "code-review",
            },
        )
        await handle.flush()

        events = _read_events(path)
        annotations = [e for e in events if e["event_type"] == "annotation"]
        assert len(annotations) == 1
        p = annotations[0]["payload"]
        assert p["intent"] == "summarize PR comments"
        assert p["expected_outcome"] == "2-3 sentence paragraph"
        assert p["workflow"] == "code-review"
        assert p["signal_type"] is None

    async def test_reactive_annotation_with_signal_type(
        self, configured_mcp: tuple[Any, Any, str]
    ) -> None:
        mcp, handle, path = configured_mcp
        await mcp.call_tool(
            "test-vendor_annotate",
            {
                "intent": "fetch the search results",
                "signal_type": "dead_end",
                "suggested_improvement": "surface clearer error",
                "context": {"likely_cause": "content_filter"},
            },
        )
        await handle.flush()

        events = _read_events(path)
        annotations = [e for e in events if e["event_type"] == "annotation"]
        assert len(annotations) == 1
        p = annotations[0]["payload"]
        assert p["intent"] == "fetch the search results"
        assert p["signal_type"] == "dead_end"
        assert p["suggested_improvement"] == "surface clearer error"
        assert p["context"]["likely_cause"] == "content_filter"

    async def test_annotation_does_not_emit_tool_call_events(
        self, configured_mcp: tuple[Any, Any, str]
    ) -> None:
        """The wrap layer MUST skip wrapping the annotation tool — its handler
        emits an annotation event, not a tool_call_start/end pair."""
        mcp, handle, path = configured_mcp
        await mcp.call_tool("test-vendor_annotate", {"intent": "x"})
        await handle.flush()

        events = _read_events(path)
        types = [e["event_type"] for e in events]
        assert types.count("annotation") == 1
        assert "tool_call_start" not in types
        assert "tool_call_end" not in types


# =============================================================================
# Full thesis flow: proactive + tool + reactive
# =============================================================================


class TestAllFourEventTypesInOneFlow:
    async def test_proactive_annotate_then_tool_call_then_reactive_annotate(
        self, configured_mcp: tuple[Any, Any, str]
    ) -> None:
        mcp, handle, path = configured_mcp

        @mcp.tool()
        def lookup(name: str) -> str:
            return f"found: {name}"

        await mcp.call_tool(
            "test-vendor_annotate",
            {"intent": "find user", "expected_outcome": "user record"},
        )
        await mcp.call_tool("lookup", {"name": "alice"})
        await mcp.call_tool(
            "test-vendor_annotate",
            {
                "intent": "find user",
                "signal_type": "dead_end",
                "suggested_improvement": "...",
            },
        )

        await handle.flush()
        events = _read_events(path)
        types = [e["event_type"] for e in events]
        assert types.count("annotation") == 2
        assert types.count("tool_call_start") == 1
        assert types.count("tool_call_end") == 1

    async def test_sequence_numbers_monotonic_across_event_types(
        self, configured_mcp: tuple[Any, Any, str]
    ) -> None:
        mcp, handle, path = configured_mcp

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        await mcp.call_tool("test-vendor_annotate", {"intent": "x"})
        await mcp.call_tool("echo", {"text": "y"})

        await handle.flush()
        events = _read_events(path)
        seqs = [e["sequence_number"] for e in events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
