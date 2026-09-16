"""Both MCP adapters must report the SAME ``transport_observed`` for the same
transport — and must report the RIGHT one.

The third file in the parity set, after ``test_agent_runtime_parity.py`` and
``test_principal_id_parity.py``, and it exists for the same recorded reason:
a fact each adapter derives for itself, from a DIFFERENT library call, where
one adapter silently reporting the wrong thing is invisible to that adapter's
own suite. Here the two reads have nothing in common —
``fastmcp.get_http_request()`` raises on absence, while the official adapter
reads ``request_context.request`` and gets ``None`` — so there is no shared
function to pin, only a shared answer.

It follows the same two rules as its siblings:

1. **Assert the EXPECTED value, not merely that the two agree.** Two adapters
   broken identically — both returning ``None``, which is exactly the state
   before this change — pass an agreement-only check.
2. **Fail when nothing was checked.** A filter matching no events would
   otherwise turn a vacuous pass into a green tick.

⚠ **A third rule this field needs and the others do not: the WRONG value is
worse than no value.** ``no-http-request`` is not a label, it is a licence —
SPEC §3.4 lets a consumer group a process-wide fallback ``session_id`` on it
and only on it. So a read that answers ``no-http-request`` when it should have
said ``read-failed`` hands out that licence on the strength of one of our own
bugs, and the console merges two strangers' conversations. That is why
``test_a_raised_read_is_read_failed_not_absence`` exists and why it asserts the
value rather than the shape.

**The HTTP cell is standalone-only, and that is a fixture gap, not a finding.**
``_running_server`` in ``tests/integrations/standalone/test_concurrent_sessions.py``
is the only fixture in this suite that puts a real socket under an MCP call;
the official adapter has no equivalent. Recorded here as SKIPPED rather than
absent — the two are different results, and the register's pass bar 3 says an
empty cell counts only when we know who emptied it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._event_helpers import without_surface_snapshots

pytestmark = pytest.mark.functional

TENANT = "tenant-transport"


def _read_events(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _transports(path: Path, *, include_snapshots: bool = False) -> set[str | None]:
    """The distinct ``transport_observed`` values across a run's events.

    Snapshots are excluded by default: ``surface_snapshot`` describes the
    SERVER, not a caller, and carries null by design — the same reason it
    carries ``UNKNOWN_AGENT_RUNTIME`` rather than a detected runtime. Folding
    it in would put a null in every set and make each assertion below pass for
    the wrong reason.
    """
    events = _read_events(path)
    if not include_snapshots:
        events = without_surface_snapshots(events)
    assert events, f"no events captured at {path} — the assertion would be vacuous"
    return {ev.get("transport_observed") for ev in events}


async def _run_official_in_memory(events_path: Path) -> None:
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink
    from tests._mcp_session import connected_session

    mcp = FastMCP("transport-official")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="transport",
            vendor_display_name="Transport Vendor",
            consent_token="ct_transport",
            sink=FileSink(str(events_path)),
            tenant_id=TENANT,
        ),
    )
    try:
        async with connected_session(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
            await client.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "look up", "signal_type": "failure"},
            )
    finally:
        await handle.aclose()


async def _run_standalone_in_memory(events_path: Path) -> None:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.sinks import FileSink

    mcp: Any = FastMCP("transport-standalone")

    @mcp.tool
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="transport",
            vendor_display_name="Transport Vendor",
            consent_token="ct_transport",
            sink=FileSink(str(events_path)),
            tenant_id=TENANT,
        ),
    )
    try:
        async with Client(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
            await client.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "look up", "signal_type": "failure"},
            )
    finally:
        await handle.aclose()


async def test_both_adapters_report_no_http_request_off_the_wire(tmp_path: Path) -> None:
    """In-memory on both adapters, and the value is the one SPEC registers.

    In-memory stands in for stdio deliberately: SPEC folds the two under one
    value, because what the field records is the ABSENCE of an HTTP request and
    neither transport has one. The suite has no stdio fixture at all — that is
    recorded in ``test_session_ladder.py``'s docstrings, not hidden here.
    """
    official = tmp_path / "official.jsonl"
    standalone = tmp_path / "standalone.jsonl"

    await _run_official_in_memory(official)
    await _run_standalone_in_memory(standalone)

    assert _transports(official) == {"no-http-request"}
    assert _transports(standalone) == {"no-http-request"}


async def test_the_surface_snapshot_carries_no_transport(tmp_path: Path) -> None:
    """A snapshot describes the SERVER, so there is no caller's transport to name.

    The precedent is ``agent_runtime``, which the snapshot emits as
    ``UNKNOWN_AGENT_RUNTIME`` rather than a detected value for the same reason.
    Pinned because the natural mistake when wiring a new envelope field is to
    set it at every emit site.
    """
    standalone = tmp_path / "standalone.jsonl"
    await _run_standalone_in_memory(standalone)

    events = _read_events(standalone)
    snapshots = [ev for ev in events if ev["event_type"] == "surface_snapshot"]
    assert snapshots, "no surface_snapshot captured — the assertion would be vacuous"
    assert {ev.get("transport_observed") for ev in snapshots} == {None}


async def test_the_standalone_adapter_sees_a_real_socket_as_http(tmp_path: Path) -> None:
    """The only end-to-end ``"http"`` assertion in the suite.

    Driven through ``_running_server``, which binds a real port, because it is
    the one fixture that puts a socket under an MCP call. The contextvar route
    (``fastmcp.server.http.set_http_request``) is deliberately NOT used here:
    ``test_session_ladder.py:89-106`` gates on whether that contextvar is
    observable at all on a given fastmcp version and SKIPS when it is not, so a
    test built on it would go quietly green-by-absence on some matrix legs. A
    real socket cannot skip.

    ⚠ **TWO clients, because the fixture's tool holds an ``asyncio.Barrier(2)``
    and only returns once both callers are inside it.** That is not a detail to
    work around — it is the exact shape this field exists for. Two callers on
    one HTTP process is the production merge (2026-09-15: a ChatGPT and a Claude
    connector on one server landed in one session), so asserting BOTH of their
    events say ``"http"`` is the assertion worth making. A single-client run
    would hang on the barrier and then read as a failure of the code under test.
    """
    import asyncio

    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from tests.integrations.standalone.test_concurrent_sessions import _running_server

    with _running_server("http") as (sink, url):

        async def _call(caller: str) -> None:
            async with Client(url) as client:
                await client.call_tool("work", {"caller": caller})

        await asyncio.gather(_call("client-a"), _call("client-b"))

        events = without_surface_snapshots([ev.model_dump() for ev in sink.events])
        assert events, "no events captured over the socket — the assertion would be vacuous"
        assert {ev.get("transport_observed") for ev in events} == {"http"}


def test_a_raised_read_is_read_failed_not_absence() -> None:
    """An unexpected exception MUST NOT read as ``no-http-request``.

    This is the whole safety argument of the field, so it is asserted directly
    on both resolvers rather than inferred from an end-to-end run. ``no-http-request``
    licenses the console to group a process-wide ``session_id``; handing that
    licence out because our own read crashed is how a producer bug becomes two
    strangers in one conversation (register A6, which is a LIVE defect in
    ``_extract_headers_from_context`` — the helper this read deliberately does
    not use).
    """
    from baton.integrations.official._tool_wrap import observe_transport as observe_official

    class _Exploding:
        @property
        def request_context(self) -> Any:
            raise AttributeError("unexpected context shape")

    assert observe_official(_Exploding()) == "read-failed"

    pytest.importorskip("fastmcp")
    from baton.integrations.standalone._session import observe_transport as observe_standalone

    def _boom() -> Any:
        raise AttributeError("fastmcp internals moved")

    assert observe_standalone(_get_http_request=_boom) == "read-failed"


def test_no_live_mcp_request_is_null_not_no_http_request() -> None:
    """A tool called straight from code has no MCP wire, so we looked at nothing.

    SPEC defines ``no-http-request`` as "a LIVE MCP request with no HTTP
    request behind it". A programmatic ``mcp.call_tool()`` is not that, so
    claiming it would assert a fact about a deployment that is not running.
    Null — "no MCP transport exists" — is the registered value for it.

    ⚠ Not ``read-failed`` either: the ``ValueError`` here is the library's
    deliberate, documented answer to "is there a request?", not our read
    breaking.
    """
    from baton.integrations.official._tool_wrap import observe_transport

    class _NoRequest:
        @property
        def request_context(self) -> Any:
            raise ValueError("Context is not available outside of a request")

    assert observe_transport(_NoRequest()) is None
    assert observe_transport(None) is None
