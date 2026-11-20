"""Tool list events (SPEC §11.4.5) that only the fastmcp adapter can show.
The cases both adapters share are in ``tests/functional/test_tool_list_parity.py``."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from baton.integrations.standalone import VendorConfig, install_baton
from baton.integrations.standalone.middleware import BatonMiddleware
from baton.sinks import FileSink
from tests._event_helpers import TOOL_LIST_EVENT_TYPES, read_events


def _install(mcp: Any, events_path: Path) -> Any:
    return install_baton(
        mcp,
        VendorConfig(
            report_mode="on",
            vendor_id="v",
            vendor_display_name="V",
            consent_token="ct",
            sink=FileSink(str(events_path)),
        ),
    )


@pytest.mark.skipif(
    "list_page_size" not in inspect.signature(FastMCP.__init__).parameters,
    reason="this fastmcp does not page its tool list",
)
async def test_a_paged_listing_sends_a_pair_per_page_counting_that_page(tmp_path: Path) -> None:
    mcp: Any = FastMCP("paged", list_page_size=2)
    for name in ("a", "b", "c", "d"):
        mcp.tool(name=name)(lambda: "ok")
    events_path = tmp_path / "events.jsonl"
    handle = _install(mcp, events_path)
    try:
        async with Client(mcp) as client:
            received = len(await client.list_tools())
    finally:
        await handle.aclose()

    assert received == 5, "four tools and Baton's own"
    listings = [ev for ev in read_events(events_path) if ev["event_type"] in TOOL_LIST_EVENT_TYPES]
    assert [ev["event_type"] for ev in listings] == ["tool_list_start", "tool_list_end"] * 3
    assert [ev["payload"]["count"] for ev in listings[1::2]] == [2, 2, 1]
    (snapshot,) = [ev for ev in read_events(events_path) if ev["event_type"] == "surface_snapshot"]
    assert len(snapshot["payload"]["tools"]) == 4
    assert {ev["payload"]["surface_hash"] for ev in listings[1::2]} == {
        snapshot["payload"]["surface_hash"]
    }


async def test_each_listing_end_names_the_snapshot_that_describes_it(tmp_path: Path) -> None:
    mcp: Any = FastMCP("hashed")
    mcp.tool(name="a")(lambda: "ok")
    events_path = tmp_path / "events.jsonl"
    handle = _install(mcp, events_path)
    try:
        async with Client(mcp) as client:
            await client.list_tools()
            await client.list_tools()
            mcp.tool(name="b")(lambda: "ok")
            await client.list_tools()
    finally:
        await handle.aclose()

    events = read_events(events_path)
    snapshots = [
        ev["payload"]["surface_hash"] for ev in events if ev["event_type"] == "surface_snapshot"
    ]
    ends = [ev["payload"]["surface_hash"] for ev in events if ev["event_type"] == "tool_list_end"]
    assert len(snapshots) == 2
    assert ends == [snapshots[0], snapshots[0], snapshots[1]], "the second listing was not re-sent"


async def test_a_listing_whose_snapshot_failed_names_none(tmp_path: Path) -> None:
    mcp: Any = FastMCP("unsent")
    mcp.tool(name="a")(lambda: "ok")
    events_path = tmp_path / "events.jsonl"
    handle = _install(mcp, events_path)
    middleware = next(m for m in mcp.middleware if isinstance(m, BatonMiddleware))

    def fail(*_args: Any) -> bool:
        raise RuntimeError("snapshot failed")

    middleware._sent_surfaces.was_sent = fail
    try:
        async with Client(mcp) as client:
            await client.list_tools()
    finally:
        await handle.aclose()

    events = read_events(events_path)
    assert not [ev for ev in events if ev["event_type"] == "surface_snapshot"]
    (end,) = [ev for ev in events if ev["event_type"] == "tool_list_end"]
    assert end["payload"]["surface_hash"] is None


async def test_a_mounted_servers_snapshot_never_names_the_parents_listing(tmp_path: Path) -> None:
    child: Any = FastMCP("child")
    child.tool(name="c")(lambda: "ok")
    parent: Any = FastMCP("parent")
    parent.tool(name="p")(lambda: "ok")
    parent.mount(child)
    child_handle = _install(child, tmp_path / "child.jsonl")
    events_path = tmp_path / "parent.jsonl"
    handle = _install(parent, events_path)
    middleware = next(m for m in parent.middleware if isinstance(m, BatonMiddleware))

    def fail(*_args: Any) -> bool:
        raise RuntimeError("snapshot failed")

    middleware._sent_surfaces.was_sent = fail
    try:
        async with Client(parent) as client:
            await client.list_tools()
    finally:
        await handle.aclose()
        await child_handle.aclose()

    assert [
        ev for ev in read_events(tmp_path / "child.jsonl") if ev["event_type"] == "surface_snapshot"
    ]
    (end,) = [ev for ev in read_events(events_path) if ev["event_type"] == "tool_list_end"]
    assert end["payload"]["surface_hash"] is None
