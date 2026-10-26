"""SPEC §3.4 rung 5 on the official adapter, over a real HTTP server.

Two clients call at the same time, and a barrier in the tool holds each call
until both are inside it, so a run that serialises them times out instead of
passing on traffic that never overlapped. In-process transports carry no HTTP
request, so only a network server shows which session each call lands in.

Uses only ``mcp``'s own client and server, plus the ``uvicorn`` that ``mcp``
requires, so it runs in the ``mcp-matrix`` leg.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from importlib.metadata import version
from typing import Any

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client

from baton.events import Event
from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass
from tests._forced_reorder import CapturingSink

try:
    from mcp.client.streamable_http import streamable_http_client as _http_client
except ImportError:  # mcp before the rename
    from mcp.client.streamable_http import (  # type: ignore[attr-defined,no-redef]
        streamablehttp_client as _http_client,
    )

MCP_MAJOR = int(version("mcp").split(".")[0])
BARRIER_TIMEOUT_S = 10.0
BOOT_TIMEOUT_S = 15.0
# A clock-numbered event (SPEC §5.6 rule 3) is microseconds since the epoch.
CLOCK_FLOOR = 10**15


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@contextlib.contextmanager
def _running_server(transport: str) -> Iterator[tuple[CapturingSink, Any, str]]:
    sink = CapturingSink()
    stateless = transport == "stateless"
    kwargs = {"stateless_http": True} if stateless and MCP_MAJOR < 2 else {}
    mcp = MCPServerClass("http-sessions", **kwargs)
    barrier = asyncio.Barrier(2)

    @mcp.tool()
    async def get_performance(platform: str) -> str:
        await asyncio.wait_for(barrier.wait(), BARRIER_TIMEOUT_S)
        return f"ok {platform}"

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="http-sessions",
            vendor_display_name="HTTP Sessions",
            consent_token="ct_http_sessions",
            sink=sink,
        ),
    )
    if transport == "sse":
        app, path = mcp.sse_app(), "/sse"
    elif stateless and MCP_MAJOR >= 2:
        app, path = mcp.streamable_http_app(stateless_http=True), "/mcp"
    else:
        app, path = mcp.streamable_http_app(), "/mcp"

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while not server.started:
        if time.monotonic() > deadline:
            raise TimeoutError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield sink, handle, f"http://127.0.0.1:{port}{path}"
    finally:
        server.should_exit = True
        thread.join(BOOT_TIMEOUT_S)


async def _call_as(transport: str, url: str, platform: str) -> None:
    client = sse_client(url) if transport == "sse" else _http_client(url)
    async with client as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            await session.call_tool("get_performance", {"platform": platform})


async def _two_concurrent_callers(transport: str) -> tuple[list[Event], Any]:
    with _running_server(transport) as (sink, handle, url):
        await asyncio.gather(
            _call_as(transport, url, "org-a-client"),
            _call_as(transport, url, "org-b-client"),
        )
    calls = [e for e in sink.events if e.event_type in {"tool_call_start", "tool_call_end"}]
    assert len(calls) == 4, [e.event_type for e in sink.events]
    return calls, handle


@pytest.mark.parametrize("transport", ["stateless", "sse"])
async def test_two_callers_with_no_session_to_read_get_one_session_per_call(
    transport: str,
) -> None:
    """SPEC §3.4 rung 5: each call is its own session, and a call's start and
    end stay together."""
    calls, handle = await _two_concurrent_callers(transport)

    by_session: dict[str, list[Event]] = {}
    for event in calls:
        by_session.setdefault(event.session_id, []).append(event)
    assert len(by_session) == 2, f"two callers shared a session: {sorted(by_session)}"
    assert handle.session_id not in by_session
    for events in by_session.values():
        assert sorted(e.event_type for e in events) == ["tool_call_end", "tool_call_start"]
        assert len({e.call_id for e in events}) == 1
        first = min(events, key=lambda e: e.sequence_number)
        assert first.event_type == "tool_call_start"
    assert {(e.transport_observed, e.session_id_origin) for e in calls} == {("http", "minted")}
    # A per-session counter would keep one entry for every request served.
    assert all(e.sequence_number > CLOCK_FLOOR for e in calls), [e.sequence_number for e in calls]


async def test_a_stateful_http_session_is_still_the_clients() -> None:
    """The healthy case: a header is read, so rung 5 never fires."""
    calls, handle = await _two_concurrent_callers("stateful")
    sessions = {e.session_id for e in calls}
    assert len(sessions) == 2
    assert handle.session_id not in sessions
    assert {e.session_id_origin for e in calls} == {"observed"}
    assert {e.sequence_number for e in calls} == {1, 2}
