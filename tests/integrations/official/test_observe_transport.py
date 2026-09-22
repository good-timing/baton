"""``observe_transport`` — the official adapter's five answers, and the WIRE.

**Why this file sits in ``tests/integrations/official/`` and not beside the
parity test.** ``mcp-matrix`` in ``ci.yml`` is the only job that pins an ``mcp``
version — 1.20, 1.25, 1.27.2, 2.0.0 — and its last step runs
``pytest tests/integrations/official/`` and nothing else. Until this file
existed that directory held **no assertion about the transport read at all**,
so none of those four legs exercised it.

⚠ **It was not that the read never RAN there.** ``_fake_context`` attaches a
real request object, so ``observe_transport`` was already being called through
the install tests on every leg — and nothing looked at what it returned.
Measured 2026-09-22: inverting the terminal return in ``_tool_wrap.py`` left
``tests/integrations/official/`` at 97 passed, exit 0, while
``tests/functional/test_transport_observed_parity.py`` reddened. A fixture that
exercises a read without asserting on it is indistinguishable from no coverage.

⚠ **The parity test cannot be moved or pointed at.** ``tests/functional/``
covers BOTH adapters, so every file in it needs ``fastmcp`` — and this job
resolves ``[mcp,test]`` and **fails on purpose if fastmcp is installed**
(``ci.yml``: "must not have it", and the leg prints ``no fastmcp``). Adding that
directory to the job's run line would break the job rather than close the gap.

⚠ **Fakes alone are NOT enough, and the first cut of this file was fakes
alone.** Every assertion below the unit block runs on a hand-built context, so
it behaves identically on all four ``mcp`` legs — which gives the version-churn
job it was written for no version-specific signal at all. Worse, it pins the
FUNCTION and not the WIRE: measured 2026-09-22, cutting ``call_transport =
observe_transport(context)`` to ``None`` in ``_tool_wrap.py`` severs the field
from the envelope entirely and leaves this directory at **107 passed, exit 0**.
``test_the_wire_carries_what_the_read_returned`` closes that, over a REAL
in-memory session through ``connected_session`` — which needs only ``mcp`` and
``anyio``, no fastmcp, and which ``test_agent_runtime.py`` in this same
directory already uses. The fakes stay: they are the only way to reach the
``read-failed`` branches, which no real session produces.

The five answers are ``SPEC §11.4``'s and the function's docstring states the
reasoning for each; this file pins them, it does not restate them.
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

    Both context shapes, because mcp 1.x has no ``.headers`` on ``Context`` and
    2.x does — the read must key on the REQUEST either way, never on headers.
    """
    assert observe_transport(ctx_class({"x-forwarded-for": "203.0.113.7"})) == "http"


@pytest.mark.parametrize("ctx_class", [_FakeContextV2, _FakeContextV1])
def test_no_request_behind_a_live_call_reads_as_no_http_request(ctx_class: Any) -> None:
    """``rc.request is None`` → ``"no-http-request"``. Stdio or in-memory.

    ⚠ This is the value SPEC §3.4 licenses a consumer to group a process-wide
    fallback ``session_id`` on. It must never be the answer to a FAILED read.
    """
    assert observe_transport(ctx_class(None)) == "no-http-request"


def test_http_and_no_http_request_are_not_interchangeable() -> None:
    """The two live answers must differ.

    Without this, inverting the terminal return is invisible to this directory
    — which is exactly what was measured on 2026-09-22.
    """
    with_request = observe_transport(_FakeContextV2({"host": "example.test"}))
    without_request = observe_transport(_FakeContextV2(None))
    assert with_request == "http"
    assert without_request == "no-http-request"
    assert with_request != without_request


def test_no_context_at_all_reads_as_none() -> None:
    """Nothing to look at → ``None``, not a claim about the deployment."""
    assert observe_transport(None) is None


def test_a_programmatic_call_reads_as_none_not_no_http_request() -> None:
    """``request_context`` raising ``ValueError`` is ``mcp.call_tool()``.

    Deliberately NOT ``no-http-request``: there is no live MCP request, so
    saying so would assert a fact about a deployment that is not running. And
    NOT ``read-failed``: nothing failed.
    """
    assert observe_transport(_RequestContextRaises(ValueError("no active request"))) is None


def test_an_unexpected_context_failure_reads_as_read_failed() -> None:
    """Any other raise from ``request_context`` → ``"read-failed"``.

    The whole point of the value: our own instrument failing must be countable,
    and must not be dressed up as a fact about the customer's transport.
    """
    assert (
        observe_transport(_RequestContextRaises(AttributeError("no such attribute")))
        == "read-failed"
    )


def test_a_failing_request_read_reads_as_read_failed() -> None:
    """The second read can fail on its own, after ``request_context`` succeeded."""
    assert observe_transport(_RequestRaises()) == "read-failed"


def test_a_context_with_no_request_context_object_reads_as_none() -> None:
    """The FIFTH answer: ``rc is None`` → ``None`` (``_tool_wrap.py:789``).

    ⚠ Unpinned until 2026-09-22, and this file's own header called it "four
    answers". Mutating that branch to ``"no-http-request"`` survived all 107
    tests — handing out the one value SPEC §3.4 licenses a consumer to group a
    process-wide fallback ``session_id`` on, for a context shape nobody has
    observed. Caught by ``/code-review``, not by me.
    """

    class _NoRequestContext:
        request_context = None

    assert observe_transport(_NoRequestContext()) is None


def test_read_failed_is_never_confused_with_absence() -> None:
    """The A6 direction: a failed read must not land on ``no-http-request``.

    ``_extract_headers_from_context`` folds ``AttributeError`` into the same
    ``None`` a genuine absence returns, and register A6 records that as a live
    defect. ``observe_transport`` must not inherit it — this is the assertion
    that would red if someone rebuilt it on that helper.
    """
    failed = observe_transport(_RequestContextRaises(AttributeError("boom")))
    absent = observe_transport(_FakeContextV2(None))
    assert failed == "read-failed"
    assert absent == "no-http-request"
    assert failed != absent


# ─── the WIRE ────────────────────────────────────────────────────────────────
# Everything above asserts what the FUNCTION returns, on a hand-built context.
# That is version-blind by construction: a fake behaves the same on mcp 1.20 and
# on 2.0.0, so it gives `mcp-matrix` nothing it could not get from one leg. The
# two tests below run the real library — so they move when the library moves,
# which is the whole reason this file is in the matrix's run line.


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
