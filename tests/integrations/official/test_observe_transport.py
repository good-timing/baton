"""``observe_transport`` — its five answers, and the wire that carries them.

``observe_transport``'s own docstring owns WHY each answer is what it is; this
file only pins them. Two things about its placement are not stated there:

**It lives here because ``mcp-matrix`` can run nothing else.** That job is the
only one pinning an ``mcp`` version (1.20 / 1.25 / 1.27.2 / 2.0.0) and its last
step runs ``pytest tests/integrations/official/`` alone. The equivalent
assertions in ``tests/functional/test_transport_observed_parity.py`` cover both
adapters, so every file there imports ``fastmcp`` — and this job resolves
``[mcp,test]`` and fails on purpose if fastmcp is present. Pointing the job at
that directory would break it, not widen it. Hence the duplication.

**The fakes cannot carry it alone.** A hand-built context behaves the same on
all four legs, so fake-only tests give a version matrix no version signal — and
they pin the function while leaving the WIRE free to be disconnected. The two
``connected_session`` tests at the bottom are what close that; the fakes stay
because they are the only route to the ``read-failed`` branches, which no real
session produces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.integrations.official._tool_wrap import observe_transport
from baton.sinks import FileSink
from tests._event_helpers import without_surface_snapshots
from tests._mcp_session import connected_session

from ._fake_context import _FakeContextV1, _FakeContextV2


class _RequestContextRaises:
    """A context whose ``request_context`` raises — the library's documented
    answer to "is there a live request?" (``ValueError``) and an unexpected
    failure (anything else)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    @property
    def request_context(self) -> Any:
        raise self._exc


class _RequestRaises:
    """``request_context`` is fine; reading ``.request`` off it is not."""

    class _RC:
        @property
        def request(self) -> Any:
            raise RuntimeError("the request read blew up")

    def __init__(self) -> None:
        self.request_context = _RequestRaises._RC()


@pytest.mark.parametrize("ctx_class", [_FakeContextV2, _FakeContextV1])
def test_a_live_request_reads_as_http(ctx_class: Any) -> None:
    """A request object is there → ``"http"``.

    ⚠ **``_FakeContextV1`` is not decoration — measured.** It has no
    ``.headers`` at all, which is mcp 1.x. Rewriting the read to consult
    ``context.headers`` — the shortcut ``observe_transport``'s docstring rejects
    by name, register A7 — reds the V1 leg and PASSES the V2 leg. V1 is the only
    fixture in this file that can tell that rewrite apart from a correct one.
    """
    assert observe_transport(ctx_class({"x-forwarded-for": "203.0.113.7"})) == "http"


@pytest.mark.parametrize("ctx_class", [_FakeContextV2, _FakeContextV1])
def test_no_request_behind_a_live_call_reads_as_no_http_request(ctx_class: Any) -> None:
    """``rc.request is None`` → ``"no-http-request"``. Stdio or in-memory."""
    assert observe_transport(ctx_class(None)) == "no-http-request"


def test_no_context_at_all_reads_as_none() -> None:
    """Nothing to look at → ``None``, not a claim about the deployment."""
    assert observe_transport(None) is None


def test_a_programmatic_call_reads_as_none_not_no_http_request() -> None:
    """``request_context`` raising ``ValueError`` is ``mcp.call_tool()``."""
    assert observe_transport(_RequestContextRaises(ValueError("no active request"))) is None


def test_an_unexpected_context_failure_reads_as_read_failed() -> None:
    """Any other raise from ``request_context`` → ``"read-failed"``."""
    assert (
        observe_transport(_RequestContextRaises(AttributeError("no such attribute")))
        == "read-failed"
    )


def test_a_failing_request_read_reads_as_read_failed() -> None:
    """The second read can fail on its own, after ``request_context`` succeeded."""
    assert observe_transport(_RequestRaises()) == "read-failed"


def test_a_context_with_no_request_context_object_reads_as_none() -> None:
    """The fifth answer: the ``rc is None`` guard → ``None``.

    ⚠ Mutating that guard to ``"no-http-request"`` survived every other test
    here — handing out the one value SPEC §3.4 licenses a consumer to group a
    process-wide fallback ``session_id`` on, for a context shape nobody has
    observed.
    """

    class _NoRequestContext:
        request_context = None

    assert observe_transport(_NoRequestContext()) is None


# ─── the WIRE: the two tests below run the real library ─────────────────────


async def _emit(events_path: Path, *, programmatic: bool) -> list[dict[str, Any]]:
    """Drive one tool call and return the envelopes the SDK actually wrote.

    ``programmatic=True`` calls through ``mcp.call_tool(...)``, which is the
    real source of the ``ValueError`` branch — the library's documented answer
    to "is there a live request?" when nobody is on the other end. Otherwise a
    real in-memory client session, via ``connected_session`` (``mcp`` + ``anyio``
    only, no fastmcp — the same helper ``test_agent_runtime.py`` uses here).
    """
    mcp = FastMCP("transport-wire")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="tw",
            vendor_display_name="Transport Wire",
            consent_token="ct_tw",
            sink=FileSink(str(events_path)),
        ),
    )
    try:
        if programmatic:
            await mcp.call_tool("lookup", {"name": "alice"})
        else:
            async with connected_session(mcp) as client:
                await client.call_tool("lookup", {"name": "alice"})
    finally:
        await handle.aclose()

    with open(events_path) as f:
        events = [json.loads(line) for line in f if line.strip()]
    events = without_surface_snapshots(events)
    assert events, "no events captured — the assertions below would be vacuous"
    return events


async def test_the_wire_carries_what_the_read_returned(tmp_path: Path) -> None:
    """The envelope's ``transport_observed``, over a REAL session.

    ⚠ **This is the one that pins the WIRE.** Every other test here pins
    ``observe_transport`` in isolation, so severing it from the envelope —
    ``call_transport = None`` in ``_tool_wrap.py`` — left the whole directory
    green at 107 passed. Only this shape reds on that cut.

    In-memory transport has no HTTP request behind a live MCP request, so the
    answer is ``no-http-request`` — NOT ``None``, which would mean we were
    handed nothing to look at, and not ``read-failed``, which would mean our own
    instrument broke. Those three being distinct is the entire point of C3.
    """
    events = await _emit(tmp_path / "wire.jsonl", programmatic=False)
    observed = {e.get("transport_observed") for e in events}
    assert observed == {"no-http-request"}, f"expected one answer on the wire, got {observed}"


async def test_a_real_programmatic_call_puts_none_on_the_wire(tmp_path: Path) -> None:
    """``mcp.call_tool()`` → ``None``, asserted against the REAL exception.

    ⚠ The ``ValueError`` answer is the most version-sensitive of the five: it is
    keyed on the library's EXCEPTION TYPE, not on a value it returns. The fake
    above hard-codes that type, so the matrix cannot see it move. This drives
    the installed library instead, so if upstream ever raises something else,
    the leg that resolved it reds — rather than every programmatic call quietly
    becoming ``"read-failed"`` with this file still green.
    """
    events = await _emit(tmp_path / "programmatic.jsonl", programmatic=True)
    observed = {e.get("transport_observed") for e in events}
    assert observed == {None}, (
        f"a programmatic call asserts nothing about transport, got {observed}"
    )
