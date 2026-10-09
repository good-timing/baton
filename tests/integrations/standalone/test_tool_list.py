"""Tool list events (SPEC §11.4.5) that only the fastmcp adapter can show.
The cases both adapters share are in ``tests/functional/test_tool_list_parity.py``."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from baton.integrations.standalone import VendorConfig, install_baton
from baton.sinks import FileSink
from tests._event_helpers import TOOL_LIST_EVENT_TYPES, read_events


@pytest.mark.skipif(
    "list_page_size" not in inspect.signature(FastMCP.__init__).parameters,
    reason="this fastmcp does not page its tool list",
)
async def test_a_paged_listing_sends_a_pair_per_page_counting_that_page(tmp_path: Path) -> None:
    mcp: Any = FastMCP("paged", list_page_size=2)
    for name in ("a", "b", "c", "d"):
        mcp.tool(name=name)(lambda: "ok")
    events_path = tmp_path / "events.jsonl"
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="v",
            vendor_display_name="V",
            consent_token="ct",
            sink=FileSink(str(events_path)),
        ),
    )
    try:
        async with Client(mcp) as client:
            received = len(await client.list_tools())
    finally:
        await handle.aclose()

    assert received == 5, "four tools and Baton's own"
    listings = [ev for ev in read_events(events_path) if ev["event_type"] in TOOL_LIST_EVENT_TYPES]
    assert [ev["event_type"] for ev in listings] == ["tool_list_start", "tool_list_end"] * 3
    assert [ev["payload"]["count"] for ev in listings[1::2]] == [2, 2, 1]
