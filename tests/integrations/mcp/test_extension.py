"""BatonExtension integration tests — official mcp SDK adapter.

Mirrors ``tests/integrations/fastmcp/test_extension.py`` but targets
``mcp.server.fastmcp.FastMCP``. Verifies all four extension channels.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from baton.extension import BatonExtension, BatonHandle
from baton.integrations.mcp import VendorConfig, install_baton
from baton.integrations.mcp.annotation import derive_annotation_tool_name
from baton.sinks import FileSink

# ---------------------------------------------------------------------------
# Test-only extension implementations
# ---------------------------------------------------------------------------


class _ToolRegExt(BatonExtension):
    def register_tools(self, mcp: Any) -> None:
        @mcp.tool(name="ext_tool_mcp", description="MCP extension tool.")
        async def ext_tool_mcp(value: str) -> dict[str, Any]:
            return {"echoed": value}


class _DescDirectiveExt(BatonExtension):
    def description_directive(self) -> str | None:
        return "IF signal_type=dead_end, MUST call create_support_ticket."


class _InstrSliceExt(BatonExtension):
    def instructions_slice(self) -> str | None:
        return "Extra MCP instructions from test extension."


class _HandleExt(BatonExtension):
    handle: BatonHandle | None = None

    def on_handle(self, handle: BatonHandle) -> None:
        self.handle = handle


# ---------------------------------------------------------------------------
# Channel 1: register_tools
# ---------------------------------------------------------------------------


async def test_extension_tool_listed(tmp_path: Any) -> None:
    events_path = str(tmp_path / "events.jsonl")
    ext = _ToolRegExt()
    mcp = FastMCP("test-vendor-mcp")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-mcp",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(events_path),
            extensions=[ext],
        ),
    )
    tool_names = {t.name for t in await mcp.list_tools()}
    assert "ext_tool_mcp" in tool_names
    await handle.aclose()


async def test_extension_tool_emits_tool_call_events(tmp_path: Any) -> None:
    """Extension tools are registered AFTER install_wraps — calls must emit events."""
    import json

    events_path = str(tmp_path / "events.jsonl")
    ext = _ToolRegExt()
    mcp = FastMCP("test-vendor-mcp")

    @mcp.tool()
    async def vendor_tool(x: str) -> str:
        return x

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-mcp",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(events_path),
            extensions=[ext],
        ),
    )

    # Call the extension tool directly via the registered handler
    await mcp.call_tool("ext_tool_mcp", {"value": "capture-me"})
    await handle.aclose()

    with open(events_path) as f:
        events = [json.loads(line) for line in f if line.strip()]

    event_types = {e["event_type"] for e in events}
    assert "tool_call_start" in event_types
    assert "tool_call_end" in event_types
    start_events = [e for e in events if e["event_type"] == "tool_call_start"]
    assert any(e["payload"]["tool_name"] == "ext_tool_mcp" for e in start_events)


# ---------------------------------------------------------------------------
# Channel 2: description_directive
# ---------------------------------------------------------------------------


async def test_description_directive_appended(tmp_path: Any) -> None:
    events_path = str(tmp_path / "events.jsonl")
    ext = _DescDirectiveExt()
    mcp = FastMCP("test-vendor-mcp")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-mcp",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(events_path),
            extensions=[ext],
        ),
    )
    annotation_name = derive_annotation_tool_name("test-mcp")
    tools = {t.name: t for t in await mcp.list_tools()}
    assert annotation_name in tools
    desc = tools[annotation_name].description or ""
    assert "IF signal_type=dead_end, MUST call create_support_ticket." in desc
    await handle.aclose()


# ---------------------------------------------------------------------------
# Channel 3: instructions_slice
# ---------------------------------------------------------------------------


def test_instructions_slice_appended(tmp_path: Any) -> None:
    events_path = str(tmp_path / "events.jsonl")
    ext = _InstrSliceExt()
    mcp = FastMCP("test-vendor-mcp")
    install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-mcp",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(events_path),
            extensions=[ext],
        ),
    )
    try:
        instructions = mcp.instructions
    except AttributeError:
        instructions = mcp._mcp_server.instructions  # type: ignore[attr-defined]
    assert "Extra MCP instructions from test extension." in (instructions or "")


# ---------------------------------------------------------------------------
# Channel 4: on_handle (session_id exposure — #89)
# ---------------------------------------------------------------------------


def test_on_handle_receives_handle_with_session_id(tmp_path: Any) -> None:
    events_path = str(tmp_path / "events.jsonl")
    ext = _HandleExt()
    mcp = FastMCP("test-vendor-mcp")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-mcp",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(events_path),
            extensions=[ext],
        ),
    )
    assert ext.handle is handle
    assert ext.handle is not None
    assert ext.handle.session_id.startswith("sdk-")


def test_handle_session_id_populated_on_returned_handle(tmp_path: Any) -> None:
    events_path = str(tmp_path / "events.jsonl")
    mcp = FastMCP("test-vendor-mcp")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-mcp",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(events_path),
        ),
    )
    assert handle.session_id.startswith("sdk-")
