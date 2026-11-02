"""The MCP install path against ``baton-spec``'s fake collector, in every
scenario of ``collector/sessions.json``; what each checks is the file's ``about``."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from mcp import ClientSession

from tests._fake_collector import each_scenario, http_sink, install_echo, received, scenario_file
from tests._mcp_session import connected_session
from tests.integrations.official.test_http_sessions import (
    BOOT_TIMEOUT_S,
    MCP_MAJOR,
    _free_port,
    _http_client,
)

_SPEC = scenario_file("sessions.json")


@contextlib.asynccontextmanager
async def _serving(mcp: Any, stateless: bool, given: set[str]) -> AsyncIterator[str]:
    """Serves ``mcp`` over streamable HTTP on this loop, which the sink shares,
    adding each Mcp-Session-Id it answers with to ``given``. Read at the server
    because ``mcp`` 2.x's client does not expose it."""
    if stateless and MCP_MAJOR >= 2:
        inner = mcp.streamable_http_app(stateless_http=True)
    else:
        inner = mcp.streamable_http_app()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        async def sending(message: Any) -> None:
            if message["type"] == "http.response.start":
                given.update(
                    v.decode() for k, v in message["headers"] if k.lower() == b"mcp-session-id"
                )
            await send(message)

        await inner(scope, receive, sending)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    serving = asyncio.ensure_future(server.serve())
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while not server.started:
        if serving.done():
            serving.result()
            raise RuntimeError("uvicorn stopped before it started")
        if time.monotonic() > deadline:
            raise TimeoutError("uvicorn did not start")
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await serving


@contextlib.asynccontextmanager
async def _session(mcp: Any, transport: str, url: str) -> AsyncIterator[ClientSession]:
    if transport == "memory":
        async with connected_session(mcp) as session:
            yield session
        return
    async with _http_client(url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            yield session


@each_scenario(_SPEC, mode="ok")
async def test_sessions_group_as_the_ladder_says(scenario: dict[str, Any], collector: int) -> None:
    transport = scenario["transport"]
    stateless = transport == "stateless_http"
    server = {"stateless_http": True} if stateless and MCP_MAJOR < 2 else {}
    mcp, handle = install_echo(http_sink(collector), **server)
    calls: list[tuple[str, str | None]] = []
    given: set[str] = set()
    serving = (
        _serving(mcp, stateless, given) if transport != "memory" else contextlib.nullcontext("")
    )
    async with serving as url:
        for c in range(scenario["clients"]):
            given.clear()
            async with _session(mcp, transport, url) as session:
                texts = [f"client {c} call {n}" for n in range(scenario["calls"])]
                for text in texts:
                    await session.call_tool("echo", {"text": text})
            # A client is given one id at most, and the next client starts after it ends.
            assert len(given) <= 1, given
            calls += [(text, next(iter(given), None)) for text in texts]
        await handle.aclose()

    events = [
        e
        for e in (await received(collector))["events"]
        if e["event_type"] in ("tool_call_start", "tool_call_end")
    ]
    expect = scenario["expect"]
    assert {(e["session_id_origin"], e["transport_observed"]) for e in events} == {
        (expect["origin"], expect["transport"])
    }
    start = {
        e["payload"]["params"]["text"]: e for e in events if e["event_type"] == "tool_call_start"
    }
    end = {e["call_id"]: e for e in events if e["event_type"] == "tool_call_end"}
    assert len(events) == 2 * len(calls) and sorted(start) == sorted(text for text, _ in calls)
    legs = [(start[text], end[start[text]["call_id"]]) for text, _ in calls]
    assert all(s["session_id"] == e["session_id"] for s, e in legs)
    ids = [s["session_id"] for s, _ in legs]
    if expect["sessions"] == "one":
        assert len(set(ids)) == 1
        numbers = [e["sequence_number"] for leg in legs for e in leg]
        assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers), numbers
    elif expect["sessions"] == "per_client":
        assert ids == [id_ for _, id_ in calls]
        assert len(set(ids)) == scenario["clients"]
    elif expect["sessions"] == "per_call":
        assert len(set(ids)) == len(calls)
    else:
        raise AssertionError(f"unknown grouping {expect['sessions']}")
