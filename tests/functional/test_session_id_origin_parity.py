"""Every event either MCP adapter sends says where its ``session_id`` came from
(SPEC §11.4). A consumer joins a stateless session only when every ``"http"``
event in it says ``"minted"``, so one emit site that leaves the field out
switches the join off for the whole session."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from tests._event_helpers import LIFECYCLE_EVENT_TYPES, TOOL_LIST_EVENT_TYPES, read_events

pytestmark = pytest.mark.functional

DOC_URI = "file:///docs/plan.txt"

Driver = Callable[[Path], Awaitable[str]]

_TOOL_CALL_AND_SERVER_TYPES = {
    "surface_snapshot",
    "tool_call_start",
    "tool_call_end",
    "tool_call_error",
    "annotation",
}
_SUCCESS_LIFECYCLE_TYPES = {t for t in LIFECYCLE_EVENT_TYPES if not t.endswith("_error")}
_SUCCESS_TOOL_LIST_TYPES = {t for t in TOOL_LIST_EVENT_TYPES if not t.endswith("_error")}


def _register(mcp: Any) -> None:
    @mcp.resource(DOC_URI)
    def doc() -> str:
        return "the plan"

    @mcp.prompt()
    def summarize(topic: str) -> str:
        return f"Summarize {topic}"

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    @mcp.tool()
    def boom() -> None:
        raise ValueError("simulated failure")


def _config(config_class: Any, events_path: Path) -> Any:
    from baton.sinks import FileSink

    return config_class(
        vendor_id="origin",
        vendor_display_name="Origin Vendor",
        consent_token="ct_origin",
        sink=FileSink(str(events_path)),
        tenant_id="tenant-origin",
    )


async def _every_request(session: Any, annotation_tool_name: str) -> None:
    await session.list_tools()
    await session.call_tool("lookup", {"name": "alice", "user_goal": "find alice"})
    await session.call_tool("boom", {})
    await session.call_tool(
        annotation_tool_name,
        {"user_goal": "look up", "what_happened": "the call came back unusable"},
    )
    await session.list_resources()
    await session.read_resource(DOC_URI)
    await session.list_prompts()
    await session.get_prompt("summarize", {"topic": "the quarter"})


async def _drive_official(events_path: Path) -> str:
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass
    from tests._mcp_session import connected_session

    mcp = MCPServerClass("origin-official")
    _register(mcp)
    handle = install_baton(mcp, _config(VendorConfig, events_path))
    try:
        async with connected_session(mcp) as session:
            await _every_request(session, handle.annotation_tool_name)
    finally:
        await handle.aclose()
    return str(handle.session_id)


async def _drive_standalone(events_path: Path) -> str:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton

    mcp: Any = FastMCP("origin-standalone")
    _register(mcp)
    handle = install_baton(mcp, _config(VendorConfig, events_path))
    try:
        async with Client(mcp) as client:
            await _every_request(client.session, handle.annotation_tool_name)
    finally:
        await handle.aclose()
    return str(handle.session_id)


ADAPTERS = [
    pytest.param(_drive_official, id="official"),
    pytest.param(_drive_standalone, id="standalone"),
]


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_every_event_off_the_wire_says_minted(drive: Driver, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    install_time_id = await drive(events_path)

    events = read_events(events_path)
    seen = {ev["event_type"] for ev in events}
    expected = _TOOL_CALL_AND_SERVER_TYPES | _SUCCESS_LIFECYCLE_TYPES | _SUCCESS_TOOL_LIST_TYPES
    assert expected <= seen, f"the run never sent {sorted(expected - seen)}"

    assert {(ev["event_type"], ev["session_id"], ev.get("session_id_origin")) for ev in events} == {
        (event_type, install_time_id, "minted") for event_type in seen
    }


@pytest.mark.parametrize(
    ("transport", "stateless"),
    [("http", False), ("http", True), ("sse", False)],
    ids=["stateful", "stateless", "sse"],
)
async def test_over_a_real_socket_the_origin_follows_the_rung(
    transport: str, stateless: bool
) -> None:
    """Two callers on one HTTP process.

    On mcp 1.x a session the client keeps (the header when stateful, the SSE
    connection) is ``"observed"``, one id per caller. Stateless, fastmcp makes
    a new id per request: ``"minted"``, one per call. On mcp 2.x no rung
    answers, so both callers share the install-time id: ``"minted"``."""
    import asyncio
    from importlib.metadata import version

    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from tests._event_helpers import without_surface_snapshots
    from tests.integrations.standalone.test_concurrent_sessions import _running_server

    mcp_major = int(version("mcp").split(".")[0])

    with _running_server(transport, stateless=stateless) as (sink, url):

        async def _call_twice(caller: str) -> None:
            async with Client(url) as client:
                await client.call_tool("work", {"caller": caller})
                await client.call_tool("work", {"caller": caller})

        await asyncio.gather(_call_twice("client-a"), _call_twice("client-b"))

        events = [ev.model_dump() for ev in sink.events]
        snapshots = [ev for ev in events if ev["event_type"] == "surface_snapshot"]
        assert snapshots, "no surface_snapshot captured, so the install-time id is unknown"
        install_time_id = snapshots[0]["session_id"]

        calls = without_surface_snapshots(events)
        assert calls, "no events captured over the socket"
        assert {ev["transport_observed"] for ev in calls} == {"http"}
        starts = [ev for ev in calls if ev["event_type"] == "tool_call_start"]
        assert len(starts) == 4
        ids_by_caller: dict[str, set[str]] = {}
        for ev in starts:
            ids_by_caller.setdefault(ev["payload"]["params"]["caller"], set()).add(ev["session_id"])
        start_ids = set().union(*ids_by_caller.values())
        origins = {ev["session_id_origin"] for ev in calls}

        if mcp_major >= 2:
            assert origins == {"minted"}
            assert start_ids == {install_time_id}
        elif stateless:
            assert origins == {"minted"}
            assert len(start_ids) == 4, "one id per request"
            assert install_time_id not in start_ids
            session_of_call = {ev["call_id"]: ev["session_id"] for ev in starts}
            for ev in calls:
                if ev.get("call_id") is not None:
                    assert ev["session_id"] == session_of_call[ev["call_id"]], ev["event_type"]
        else:
            assert origins == {"observed"}
            assert [len(ids) for ids in ids_by_caller.values()] == [1, 1], "one id per caller"
            assert len(start_ids) == 2
            assert install_time_id not in start_ids
