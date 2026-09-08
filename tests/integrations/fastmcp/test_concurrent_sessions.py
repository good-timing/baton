"""Two concurrent clients on ONE server must not share a ``session_id``.

The absence of this test is why the SSE regression in ``67c8eb2`` reached review:
every existing session test drives ONE client, and a merge is invisible with one
client — the id is stable and plausible either way. Measured 2026-09-07 (see
``baton-internal`` `mcp_integration_seams.md` §Validation V2): two clients, one
server, and 2 of 8 call pairs were attributed to the wrong caller.

Two properties this file is built around, both learned the hard way:

**The overlap has to be forced, not hoped for.** The tool waits on a barrier that
only releases once BOTH clients are inside it, so a run that accidentally
serialises the clients deadlocks and fails on the timeout instead of passing on
traffic that never overlapped.

**It has to run over a real transport.** In-process and stdio carry no session
header at all, so they merge by construction and would "detect" a bug that is not
the deployed behaviour. Only a network transport distinguishes the cases.
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
from fastmcp import Client, FastMCP

from baton.events import Event
from baton.integrations.fastmcp import VendorConfig, install_baton
from baton.sinks import Sink

# Same discriminator the fix gates on, for the same reason: mcp owns the
# ``ServerSession`` whose lifetime decides whether fastmcp's cached id survives.
# See ``baton.integrations.fastmcp._session._session_cache_survives``.
MCP_MAJOR = int(version("mcp").split(".")[0])

BARRIER_TIMEOUT_S = 10.0
BOOT_TIMEOUT_S = 15.0


class _CapturingSink(Sink):
    """Keeps every envelope in memory. The assertions are about the envelope's
    ``session_id``, so nothing needs to be serialised or shipped."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def write(self, event: Event) -> None:
        self.events.append(event)

    async def flush(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@contextlib.contextmanager
def _running_server(
    transport: str, *, stateless: bool = False
) -> Iterator[tuple[_CapturingSink, str]]:
    sink = _CapturingSink()
    mcp: FastMCP[Any] = FastMCP("concurrency-probe")
    barrier = asyncio.Barrier(2)

    @mcp.tool
    async def work(caller: str) -> str:
        """Returns only once both callers are inside it simultaneously."""
        async with asyncio.timeout(BARRIER_TIMEOUT_S):
            await barrier.wait()
        return f"done for {caller}"

    install_baton(
        mcp,
        VendorConfig(
            vendor_id="concurrency-probe",
            vendor_display_name="Concurrency Probe",
            consent_token="test-token",
            sink=sink,
        ),
    )

    port = _free_port()
    boot_error: list[BaseException] = []

    # Driven through ``run_async`` on a loop this fixture owns, rather than the
    # blocking ``mcp.run``, for one reason: ``run`` builds its own loop inside
    # uvicorn and hands back no handle, so there is nothing to stop afterwards.
    # Each parametrised case then left a live daemon thread holding a bound
    # port, an event loop, and — through the installed middleware — its sink,
    # for the rest of the pytest session.
    loop_box: list[asyncio.AbstractEventLoop] = []

    def _serve() -> None:
        loop = asyncio.new_event_loop()
        loop_box.append(loop)
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                mcp.run_async(
                    transport=transport,
                    host="127.0.0.1",
                    port=port,
                    show_banner=False,
                    stateless_http=stateless,
                )
            )
        except BaseException as exc:  # re-raised on the test thread
            boot_error.append(exc)
        finally:
            # Cancel what uvicorn left in flight before closing, or the
            # interpreter prints "Task was destroyed but it is pending!" —
            # noise that reads like a defect in the code under test.
            with contextlib.suppress(Exception):
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            with contextlib.suppress(Exception):
                loop.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while True:
        if boot_error:
            # A transport this fastmcp does not offer is a skip; anything else is
            # a real failure. Never let the two look alike.
            exc = boot_error[0]
            if isinstance(exc, ValueError) and "transport" in str(exc).lower():
                pytest.skip(f"fastmcp {version('fastmcp')} has no {transport!r} transport: {exc}")
            raise exc
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
            break
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"{transport} server did not accept connections in {BOOT_TIMEOUT_S}s"
            )
        time.sleep(0.05)

    path = "/sse/" if transport == "sse" else "/mcp/"
    try:
        yield sink, f"http://127.0.0.1:{port}{path}"
    finally:
        # Stop the loop from this thread, then wait for the server thread to
        # unwind so the port is actually free before the next case picks one.
        # Bounded: a hung shutdown must not convert a passing test into a hang.
        for loop in loop_box:
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=BOOT_TIMEOUT_S)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "transport",
    [
        pytest.param(
            "http",
            marks=pytest.mark.xfail(
                MCP_MAJOR >= 2,
                strict=True,
                raises=AssertionError,
                reason=(
                    "fastmcp 4 sends no mcp-session-id and its own cached id is "
                    "rebuilt per request, so every client lands on the process-wide "
                    "fallback and merges — seams note D2, undecided"
                ),
            ),
        ),
        pytest.param(
            "sse",
            marks=pytest.mark.xfail(
                MCP_MAJOR >= 2,
                strict=True,
                raises=AssertionError,
                reason=(
                    "SSE never sends the mcp-session-id header, and on mcp 2.x "
                    "rung 4b is gated off because the cache does not survive — "
                    "so nothing is left but the fallback"
                ),
            ),
        ),
    ],
)
async def test_concurrent_clients_do_not_share_a_session_id(transport: str) -> None:
    with _running_server(transport) as (sink, url):

        async def call_as(caller: str) -> None:
            async with Client(url) as client:
                await client.call_tool("work", {"caller": caller})

        await asyncio.gather(call_as("alice"), call_as("bob"))

    starts = [e for e in sink.events if e.event_type == "tool_call_start"]
    assert len(starts) == 2, f"expected one start per client, got {len(starts)}"

    callers = {e.payload.params.get("caller") for e in starts}  # type: ignore[union-attr]
    assert callers == {"alice", "bob"}, f"both clients must have run, saw {callers}"

    sessions = {e.session_id for e in starts}
    assert len(sessions) == 2, (
        f"two concurrent clients shared a session_id ({sessions}) — their events "
        "interleave, and a FIFO pair join attributes one caller's result to the "
        "other's arguments"
    )


@pytest.mark.anyio
@pytest.mark.xfail(
    MCP_MAJOR >= 2,
    strict=True,
    raises=AssertionError,
    reason=(
        "on mcp 2.x rung 4b is gated off (the cached id does not survive), and "
        "stateless mode issues no mcp-session-id — so both clients land on the "
        "process-wide fallback and merge. Same D2 gap as the other two legs."
    ),
)
async def test_stateless_http_clients_do_not_merge() -> None:
    """Stateless streamable HTTP splits one client's calls, and that is DELIBERATE.

    A stateless server rebuilds its session per request, so ``Context.session_id``
    (rung 4b) mints a fresh id per call: one client's consecutive calls do not
    share an id, ``sequence_number`` restarts at 1, and a cross-request
    ``*_annotate`` cannot be joined to the call it describes. That is unchanged
    from 0.6.1, which resolved through the same property — this ladder neither
    caused it nor repairs it, and SPEC §3.4's answer is rung 5 (per-event UUID),
    unbuilt in both adapters (D2).

    **What this test pins is the direction, not the split.** The tempting "fix" is
    to narrow rung 4b's gate so stateless falls through to ``fallback``. That
    would trade a recoverable failure for an unrecoverable one: a split costs
    joins, while the process-wide fallback would merge every client of a
    multi-user stateless server and attach one user's arguments to another
    user's result. This test goes RED on exactly that change, and stays green
    under rung 5, whose per-event ids are also distinct.
    """
    with _running_server("http", stateless=True) as (sink, url):

        async def call_as(caller: str) -> None:
            async with Client(url) as client:
                await client.call_tool("work", {"caller": caller})

        await asyncio.gather(call_as("alice"), call_as("bob"))

    starts = [e for e in sink.events if e.event_type == "tool_call_start"]
    assert len(starts) == 2, f"expected one start per client, got {len(starts)}"

    sessions = {e.session_id for e in starts}
    assert len(sessions) == 2, (
        f"two concurrent stateless clients shared a session_id ({sessions}) — "
        "rung 4b was narrowed and they fell through to the process-wide "
        "fallback, which merges strangers instead of merely losing their joins"
    )
