"""BatonExtension integration tests — fastmcp adapter.

Verifies all four extension channels through install_baton:
  1. register_tools — extension tool is callable.
  2. description_directive — appended to annotation tool description.
  3. instructions_slice — appended to server instructions.
  4. on_handle — receives BatonHandle with session_id populated (#89).
"""

from __future__ import annotations

from typing import Any

from fastmcp import Client, FastMCP
from pytest_httpserver import HTTPServer

from baton.extension import BatonExtension, BatonHandle
from baton.integrations.fastmcp import VendorConfig, install_baton
from baton.integrations.fastmcp.annotation import derive_annotation_tool_name
from baton.sinks import HttpSink, StdoutSink

# ---------------------------------------------------------------------------
# Test-only extension implementations
# ---------------------------------------------------------------------------


class _ToolRegExt(BatonExtension):
    """Registers one extra tool; records calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def register_tools(self, mcp: Any) -> None:
        @mcp.tool(name="ext_tool", description="Extension tool.")
        async def ext_tool(value: str) -> dict[str, Any]:
            self.calls.append({"value": value})
            return {"echoed": value}


class _DescDirectiveExt(BatonExtension):
    def description_directive(self) -> str | None:
        return "IF signal_type=failure, MUST offer to file a ticket."


class _InstrSliceExt(BatonExtension):
    def instructions_slice(self) -> str | None:
        return "Extra instructions from test extension."


class _HandleExt(BatonExtension):
    handle: BatonHandle | None = None

    def on_handle(self, handle: BatonHandle) -> None:
        self.handle = handle


# ---------------------------------------------------------------------------
# Channel 1: register_tools
# ---------------------------------------------------------------------------


async def test_extension_tool_is_callable() -> None:
    ext = _ToolRegExt()
    mcp = FastMCP("test-vendor")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=StdoutSink(),
            extensions=[ext],
        ),
    )

    async with Client(mcp) as client:
        await client.call_tool("ext_tool", {"value": "hello"})

    assert ext.calls == [{"value": "hello"}]
    await handle.aclose()


async def test_extension_tool_listed(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
    ext = _ToolRegExt()
    mcp = FastMCP("test-vendor")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
            extensions=[ext],
        ),
    )
    tool_names = {t.name for t in await mcp.list_tools()}
    assert "ext_tool" in tool_names
    await handle.aclose()


async def test_extension_tool_emits_tool_call_events(httpserver: HTTPServer) -> None:
    """Extension tools are registered AFTER middleware — calls must emit events."""
    captured: list[dict[str, Any]] = []

    def ingest_handler(request: Any) -> Any:
        from werkzeug.wrappers import Response

        captured.append(request.get_json())
        return Response("", status=201)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(ingest_handler)
    ext = _ToolRegExt()
    mcp = FastMCP("test-vendor")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=HttpSink(url=httpserver.url_for(""), api_key="k"),
            extensions=[ext],
        ),
    )

    async with Client(mcp) as client:
        await client.call_tool("ext_tool", {"value": "capture-me"})

    await handle.aclose()
    event_types = {e["event_type"] for e in captured}
    assert "tool_call_start" in event_types
    assert "tool_call_end" in event_types
    start_events = [e for e in captured if e["event_type"] == "tool_call_start"]
    assert any(e["payload"]["tool_name"] == "ext_tool" for e in start_events)


# ---------------------------------------------------------------------------
# Channel 2: description_directive
# ---------------------------------------------------------------------------


async def test_description_directive_appended() -> None:
    ext = _DescDirectiveExt()
    mcp = FastMCP("test-vendor")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=StdoutSink(),
            extensions=[ext],
        ),
    )

    annotation_name = derive_annotation_tool_name("test")
    tools = {t.name: t for t in await mcp.list_tools()}
    assert annotation_name in tools
    desc = tools[annotation_name].description or ""
    assert "IF signal_type=failure, MUST offer to file a ticket." in desc
    await handle.aclose()


async def test_description_directive_absent_without_extension() -> None:
    mcp = FastMCP("test-vendor")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=StdoutSink(),
        ),
    )
    annotation_name = derive_annotation_tool_name("test")
    tools = {t.name: t for t in await mcp.list_tools()}
    desc = tools[annotation_name].description or ""
    assert "MUST offer to file a ticket" not in desc
    await handle.aclose()


# ---------------------------------------------------------------------------
# Channel 3: instructions_slice
# ---------------------------------------------------------------------------


def test_instructions_slice_appended() -> None:
    ext = _InstrSliceExt()
    mcp = FastMCP("test-vendor")
    install_baton(
        mcp,
        VendorConfig(
            vendor_id="test",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=StdoutSink(),
            extensions=[ext],
        ),
    )

    try:
        instructions = mcp.instructions
    except AttributeError:
        instructions = mcp._mcp_server.instructions  # type: ignore[attr-defined]

    assert "Extra instructions from test extension." in (instructions or "")


# ---------------------------------------------------------------------------
# Channel 4: on_handle (session_id exposure — #89)
# ---------------------------------------------------------------------------


def test_on_handle_receives_handle_with_session_id() -> None:
    ext = _HandleExt()
    mcp = FastMCP("test-vendor")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=StdoutSink(),
            extensions=[ext],
        ),
    )

    assert ext.handle is handle
    assert ext.handle is not None
    assert ext.handle.session_id.startswith("sdk-")


def test_handle_session_id_populated_on_returned_handle() -> None:
    mcp = FastMCP("test-vendor")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=StdoutSink(),
        ),
    )
    assert handle.session_id.startswith("sdk-")


def test_on_handle_called_for_each_extension() -> None:
    ext1, ext2 = _HandleExt(), _HandleExt()
    mcp = FastMCP("test-vendor")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=StdoutSink(),
            extensions=[ext1, ext2],
        ),
    )
    assert ext1.handle is handle
    assert ext2.handle is handle
    assert ext1.handle.session_id == ext2.handle.session_id  # type: ignore[union-attr]
