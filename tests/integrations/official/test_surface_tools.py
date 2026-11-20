"""The tools in ``surface_snapshot`` carry what ``tools/list`` shows of each tool."""

from __future__ import annotations

from typing import Any

from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.sinks import FileSink
from tests._event_helpers import read_events
from tests._mcp_session import connected_session


class TestSnapshotOutputSchema:
    """The snapshot lists a tool's ``outputSchema`` as ``tools/list`` does, and
    leaves the key out for a tool without one."""

    async def test_output_schema_is_in_the_snapshot_only_when_declared(self, tmp_path: Any) -> None:
        path = str(tmp_path / "events.jsonl")
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool()
        def structured(text: str) -> dict[str, str]:
            return {"text": text}

        @mcp.tool(structured_output=False)
        def plain(text: str) -> str:
            return text

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(path),
            ),
        )
        try:
            async with connected_session(mcp) as session:
                wire = {
                    t.name: t.model_dump(by_alias=True, exclude_none=True)
                    for t in (await session.list_tools()).tools
                }
                await session.call_tool("plain", {"text": "a"})
            await handle.flush()
        finally:
            await handle.aclose()

        snapshot = next(e for e in read_events(path) if e["event_type"] == "surface_snapshot")
        tools = {t["name"]: t for t in snapshot["payload"]["tools"]}
        assert tools["structured"]["outputSchema"] == wire["structured"]["outputSchema"]
        assert tools["structured"]["outputSchema"]
        assert "outputSchema" not in tools["plain"]
