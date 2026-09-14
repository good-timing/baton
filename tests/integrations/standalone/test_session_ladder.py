"""SPEC §3.4 session-id ladder — standalone ``fastmcp`` adapter.

Parity coverage for the mcp adapter's ``TestSessionIdMetaRungs``. Before this
adapter grew a ladder it resolved only via fastmcp's ``Context.session_id``,
which SPEC calls rung 4 and which fastmcp 4.x mints fresh per request — see
``baton.integrations.standalone._session``.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.http import set_http_request
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.integrations.standalone import _session
from baton.integrations.standalone._session import resolve_call_session_id
from baton.integrations.standalone.middleware import BatonMiddleware
from baton.sinks import HttpSink, Sink
from tests._asgi import fake_http_request


@pytest.fixture
async def captured() -> list[dict[str, Any]]:
    """Per-test list collecting ingested event JSON bodies."""
    return []


@pytest.fixture
async def sink(httpserver: HTTPServer, captured: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def handler(request: Any) -> Response:
        captured.append(request.get_json())
        return Response("", status=201)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
    s = HttpSink(url=httpserver.url_for(""), api_key="k")
    yield s
    await s.aclose()


def _build_ladder_mcp(sink: Sink) -> FastMCP:
    mcp = FastMCP("test-vendor")
    mcp.add_middleware(
        BatonMiddleware(
            tenant_id="ten_test",
            vendor_id="ten_test",
            consent_token="ct_test",
            sink=sink,
        )
    )
    return mcp


async def _session_id_from_a_call(
    sink: Sink, captured: list[dict[str, Any]], *, meta: dict[str, Any]
) -> str:
    """Drive one real tool call carrying ``meta`` on the wire; return the
    ``session_id`` the middleware resolved for it."""
    mcp = _build_ladder_mcp(sink)

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    async with Client(mcp) as client:
        await client.call_tool("echo", {"text": "x"}, meta=meta)
    await sink.flush()
    start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
    return str(start["session_id"])


class _FakeContext:
    """Stands in for fastmcp's ``Context``; only ``session_id`` is read."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def _http_injection_works() -> bool:
    """Whether THIS fastmcp version's ``set_http_request`` is observable through
    ``get_http_headers``.

    It isn't on fastmcp 2.12/2.13: the two use different contextvars there, so
    the injection lands somewhere the reader never looks and every header rung
    reads empty. That is a limitation of the test harness on those versions,
    NOT of rung 4 — verified against a real fastmcp 2.12 streamable-HTTP server,
    where `mcp-session-id` is read correctly and holds stable across calls.
    Skipping beats asserting a false negative or silently dropping the coverage
    on the versions where it does work.
    """
    with set_http_request(fake_http_request([(b"mcp-session-id", b"probe")])):
        return (get_http_headers(include_all=True) or {}).get("mcp-session-id") == "probe"


requires_http_injection = pytest.mark.skipif(
    not _http_injection_works(),
    reason="fastmcp's set_http_request is not observable via get_http_headers on this version",
)


#: Shared with every other ASGI-scope site in the suite; see ``tests/_asgi``.
_fake_http_request = fake_http_request


async def _resolve(
    *,
    headers: dict[str, str] | None = None,
    fallback: str = "sdk-fallback",
) -> str:
    """⚠ Takes no ``meta``. It used to, and the retired-rung tests passed one
    to prove the ladder ignored it — which became VACUOUS on 2026-09-12 when
    rung 0's removal made ``meta`` stop being a parameter of
    ``resolve_call_session_id`` at all. A test that hands a value to a
    function that cannot accept it proves nothing about the function. The
    retired rungs are now asserted through the real middleware, below."""

    async def call() -> str:
        return await resolve_call_session_id(fallback=fallback)

    if headers is None:
        return await call()
    with set_http_request(_fake_http_request(headers)):
        return await call()


class TestTheRetiredMetaRungs:
    """Rungs 1-2 were RETIRED 2026-09-09 — ``_meta.traceparent`` and
    ``_meta["io.baton/session_id"]`` no longer resolve the session.

    Both keyed the session on an identifier the SDK did not mint, which the D2
    join rule forbids. These tests are the inverse of the ones they replace: a
    silent restoration of either rung would change what a session groups on for
    every event, so absence is asserted rather than assumed.

    ⚠ **Driven through the real middleware, on purpose.** Until 2026-09-12
    these called ``resolve_call_session_id`` with a ``meta=`` argument. Rung 0
    was that function's last reader of the wire ``_meta``, so its removal took
    the parameter — and the tests kept passing a value the function no longer
    had, which is the definition of assumed rather than asserted. The
    restoration path they exist to catch is still open: ``_session`` already
    imports ``get_context()`` and uses it at rung 4b, so a new meta rung needs
    no signature change to reach ``_meta``. Only a test that puts meta on the
    WIRE can see that, so these send it through an in-process client and
    assert on the emitted envelope.
    """

    async def test_traceparent_alone_leaves_the_fallback(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        assert (
            await _session_id_from_a_call(sink, captured, meta={"traceparent": TRACEPARENT})
            != TRACEPARENT.split("-")[1]
        )

    async def test_io_baton_session_id_alone_leaves_the_fallback(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        assert (
            await _session_id_from_a_call(
                sink, captured, meta={"io.baton/session_id": "vendor-app-handle"}
            )
            != "vendor-app-handle"
        )

    async def test_neither_key_resolves_the_session_even_together(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        got = await _session_id_from_a_call(
            sink,
            captured,
            meta={"traceparent": TRACEPARENT, "io.baton/session_id": "vendor-app-handle"},
        )
        assert got != "vendor-app-handle"
        assert got != TRACEPARENT.split("-")[1]

    async def test_the_keys_still_REACH_us_as_data(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """Retired means not keyed on, not uncaptured — the negative control
        for the three above. Without this, a middleware that dropped ``_meta``
        entirely would satisfy every one of them."""
        mcp = _build_ladder_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo",
                {"text": "x"},
                meta={"traceparent": TRACEPARENT, "io.baton/session_id": "vendor-app-handle"},
            )
        await sink.flush()
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["runtime_meta"]["traceparent"] == TRACEPARENT
        assert start["runtime_meta"]["io.baton/session_id"] == "vendor-app-handle"


class TestHeaderAndFallbackRungs:
    @requires_http_injection
    async def test_header_used_when_nothing_above_it_answers(self) -> None:
        assert await _resolve(headers={"mcp-session-id": "hdr"}) == "hdr"

    async def test_fallback_when_nothing_is_observable(self) -> None:
        """The stdio shape: no HTTP request, no meta. One process is one
        client, so the process-wide fallback is the right answer here."""
        assert await _resolve() == "sdk-fallback"

    @requires_http_injection
    async def test_empty_header_value_falls_through_to_fallback(self) -> None:
        assert await _resolve(headers={"mcp-session-id": ""}) == "sdk-fallback"

    @requires_http_injection
    async def test_unrelated_headers_do_not_resolve(self) -> None:
        assert await _resolve(headers={"x-request-id": "nope"}) == "sdk-fallback"


class TestRung4bFastmcpContext:
    """Rung 4b — fastmcp's own ``Context.session_id``, below the header.

    It exists for ONE transport: SSE never sends ``mcp-session-id`` (the id
    rides a query param), so header-only resolution dropped every SSE client
    onto the process-wide fallback and merged them. Measured with two concurrent
    clients on one server: one shared id, 2 of 8 call pairs attributed to the
    wrong caller. The rung is gated because on mcp 2.x the cached id is rebuilt
    per request — using it there would restore the per-call churn this ladder
    was built to end.
    """

    @requires_http_injection
    async def test_used_on_an_http_request_with_no_session_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SSE shape: a live HTTP request, but no ``mcp-session-id`` on it."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("per-connection-id"))
        assert await _resolve(headers={"host": "example.invalid"}) == "per-connection-id"

    async def test_not_used_without_headers_so_stdio_keeps_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stdio / in-process carry no headers. One process is one client and
        ``fallback`` already says so; firing here would mint a second
        per-process id that disagrees with the install-time
        ``surface_snapshot``'s, splitting a session from its own surface."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("a-different-uuid"))
        assert await _resolve() == "sdk-fallback"

    @requires_http_injection
    async def test_header_outranks_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("per-connection-id"))
        assert await _resolve(headers={"mcp-session-id": "from-header"}) == "from-header"

    @requires_http_injection
    async def test_answers_the_sse_shape_that_meta_used_to_take(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before the rung-1/2 retirement a ``traceparent`` beat this rung. It
        no longer does, so 4b now answers the SSE shape even when the client
        propagates a trace context — the intended widening, not a regression:
        4b is an id the server library owns.

        ⚠ Renamed 2026-09-12 from ``test_meta_no_longer_outranks_it``, which
        stopped being what it did: the meta argument it made that claim with
        went away with rung 0, leaving the old name asserting something the
        body no longer touches. The meta claim now lives in
        ``TestTheRetiredMetaRungs``, on the wire, where it can fail."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("per-connection-id"))
        got = await _resolve(headers={"host": "x"})
        assert got == "per-connection-id"

    @requires_http_injection
    async def test_gated_off_where_the_cache_does_not_survive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On mcp 2.x the id is minted fresh per call, so the rung must not fire
        — the fallback loses joins, but this rung would MANUFACTURE them."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", False)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("fresh-every-call"))
        assert await _resolve(headers={"host": "x"}) == "sdk-fallback"

    @requires_http_injection
    async def test_no_active_context_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Outside a request ``get_context`` raises; a correlation rung must
        never be able to fail the tool call it is describing."""

        def boom() -> Any:
            raise RuntimeError("No active context found.")

        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", boom)
        assert await _resolve(headers={"host": "x"}) == "sdk-fallback"

    async def test_gate_matches_the_installed_mcp_major(self) -> None:
        """The gate is a claim about the INSTALLED stack, so pin it to that
        rather than to a hardcoded expectation — this is the assertion that
        would fail if fastmcp and mcp ever stopped moving in lockstep."""
        major = int(version("mcp").split(".")[0])
        assert _session._session_cache_survives() is (major < 2)
