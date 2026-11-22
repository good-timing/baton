"""The tools in ``surface_snapshot`` carry what ``tools/list`` shows of each tool."""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.integrations.official._tool_wrap import _annotations_json
from baton.sinks import FileSink
from tests._event_helpers import read_events
from tests._mcp_session import connected_session


class TestSnapshotToolFields:
    """The snapshot lists a tool's ``outputSchema``, ``title`` and ``annotations``
    as ``tools/list`` does, and leaves each key out for a tool without it."""

    async def test_optional_fields_are_in_the_snapshot_only_when_declared(
        self, tmp_path: Any
    ) -> None:
        path = str(tmp_path / "events.jsonl")
        mcp = FastMCP("test-vendor-mcp")

        @mcp.tool(title="Create a ticket", annotations=ToolAnnotations(readOnlyHint=False))
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
        assert tools["structured"]["title"] == wire["structured"]["title"]
        assert tools["structured"]["annotations"] == wire["structured"]["annotations"]
        assert tools["structured"]["annotations"] == {"readOnlyHint": False}
        for key in ("outputSchema", "title", "annotations"):
            assert key not in tools["plain"]


class TestUnreadableAnnotations:
    def test_unreadable_annotations_are_left_out(self) -> None:
        class Broken:
            def model_dump(self, **_: Any) -> dict[str, Any]:
                raise ValueError("broken")

        class NotADict:
            def model_dump(self, **_: Any) -> list[str]:
                return ["readOnlyHint"]

        assert _annotations_json(Broken()) is None
        assert _annotations_json(NotADict()) is None
