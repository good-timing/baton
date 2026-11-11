"""A vendor tool with the report tool's name: the vendor's is kept, Baton's is
not added, and the instructions do not point at it."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from baton.sinks import FileSink

NAME = "acme_annotate"


def _config(module: Any, tmp_path: Path) -> Any:
    return module.VendorConfig(
        vendor_id="srv",
        vendor_display_name="Acme",
        tenant_id="ten_1",
        sink=FileSink(str(tmp_path / "e.jsonl")),
        report_mode="on",
        annotation_tool_name=NAME,
    )


def _clashes(caplog: pytest.LogCaptureFixture) -> int:
    return sum("report tool's name" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_the_official_adapter_keeps_the_vendors_tool(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from baton.integrations import official
    from baton.integrations.official._compat import MCPServerClass
    from tests._mcp_session import connected_session

    mcp = MCPServerClass("Acme")

    @mcp.tool(name=NAME, description="Mine.")
    def mine() -> str:
        return "the vendor's tool ran"

    with caplog.at_level(logging.WARNING):
        handle = official.install_baton(mcp, _config(official, tmp_path))
    try:
        async with connected_session(mcp) as session:
            tools = (await session.list_tools()).tools
            assert [(t.name, t.description) for t in tools] == [(NAME, "Mine.")]
            result = await session.call_tool(NAME, {})
            assert "the vendor's tool ran" in str(result.content)
        assert NAME not in str(mcp.instructions)
        assert _clashes(caplog) == 1
    finally:
        await handle.aclose()


@pytest.mark.anyio
async def test_the_standalone_adapter_keeps_the_vendors_tool(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fastmcp = pytest.importorskip("fastmcp")
    from baton.integrations import standalone

    mcp = fastmcp.FastMCP("Acme")

    @mcp.tool(name=NAME, description="Mine.")
    def mine() -> str:
        return "the vendor's tool ran"

    with caplog.at_level(logging.WARNING):
        handle = standalone.install_baton(mcp, _config(standalone, tmp_path))
    try:
        async with fastmcp.Client(mcp) as client:
            tools = await client.list_tools()
            assert [(t.name, t.description) for t in tools] == [(NAME, "Mine.")]
            result = await client.call_tool(NAME, {})
            assert "the vendor's tool ran" in str(result.content)
        assert NAME not in str(mcp.instructions)
        assert _clashes(caplog) == 1
    finally:
        await handle.aclose()
