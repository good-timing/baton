"""SPEC §3.4 session-id ladder — standalone ``fastmcp`` adapter. See
``baton.integrations.standalone._session``."""

from __future__ import annotations

from importlib.metadata import version
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.http import set_http_request
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.integrations.standalone import _session
from baton.integrations.standalone._session import (
    extract_headers,
    observe_transport,
    resolve_call_session,
)
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
    """Stands in for fastmcp's ``Context``; only ``session_id`` and the mcp
    session's ``client_params`` are read."""

    def __init__(self, session_id: str = "fastmcp-id", *, initialized: bool = False) -> None:
        self.session_id = session_id
        session = SimpleNamespace(client_params=object() if initialized else None)
        self.request_context = SimpleNamespace(session=session)


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


#: What ``_resolve`` returns for a rung 5 id, which is new on every call.
PER_CALL = "<per-call>"


async def _resolve(
    *,
    headers: dict[str, str] | None = None,
    fallback: str = "sdk-fallback",
) -> tuple[str, str]:
    """Takes no ``meta``: ``resolve_call_session`` has no meta parameter, so
    passing one would prove nothing. The meta rungs' absence is asserted
    through the real middleware, below."""

    async def call() -> tuple[str, str]:
        session = await resolve_call_session(
            headers=extract_headers(),
            fallback=fallback,
            transport_observed=observe_transport(),
        )
        if session.per_call and session.id.startswith("sdk-"):
            assert session.id != fallback
            return PER_CALL, session.origin
        return session.id, session.origin

    if headers is None:
        return await call()
    with set_http_request(_fake_http_request(headers)):
        return await call()


class TestTheRetiredMetaRungs:
    """``_meta.traceparent`` and ``_meta["io.baton/session_id"]`` do not
    resolve the session: each would key it on an id the SDK did not mint,
    which the D2 join rule forbids.

    ⚠ **Driven through the real middleware, on purpose.** ``_session`` already
    calls ``get_context()`` at rung 4b, so a new meta rung could reach
    ``_meta`` with no signature change. Only a test that puts meta on the WIRE
    and reads the emitted envelope can see that.
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
        assert await _resolve(headers={"mcp-session-id": "hdr"}) == ("hdr", "observed")

    async def test_fallback_when_nothing_is_observable(self) -> None:
        """The stdio shape: no HTTP request, no meta. One process is one
        client, so the process-wide fallback is the right answer here."""
        assert await _resolve() == ("sdk-fallback", "minted")

    @requires_http_injection
    async def test_empty_header_value_falls_through_to_rung_5(self) -> None:
        assert await _resolve(headers={"mcp-session-id": ""}) == (PER_CALL, "minted")

    @requires_http_injection
    async def test_unrelated_headers_do_not_resolve(self) -> None:
        assert await _resolve(headers={"x-request-id": "nope"}) == (PER_CALL, "minted")


@pytest.mark.parametrize(
    ("context", "scope"),
    [
        (_FakeContext(initialized=True), "connection"),
        (_FakeContext(initialized=False), "request"),
        (object(), "unknown"),
    ],
    ids=["initialized-sse-connection", "stateless-request", "unreadable"],
)
def test_the_session_check(context: Any, scope: str) -> None:
    assert _session._session_scope(context) == scope


class TestRung4bFastmcpContext:
    """Rung 4b — fastmcp's own ``Context.session_id``, below the header.

    It exists for SSE, which never sends ``mcp-session-id`` (the id rides a
    query parameter), so header-only resolution would put every SSE client on
    the process-wide fallback and merge them. It is gated off on mcp 2, where
    the cached id is rebuilt per request.
    """

    @requires_http_injection
    @pytest.mark.parametrize(
        ("scope", "origin"),
        [("connection", "observed"), ("request", "minted")],
        ids=["sse", "stateless"],
    )
    async def test_used_on_an_http_request_with_no_session_header(
        self, monkeypatch: pytest.MonkeyPatch, scope: str, origin: str
    ) -> None:
        """On SSE fastmcp's id holds for the connection; on stateless streamable
        HTTP it makes one per request."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "_session_scope", lambda _context: scope)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("fastmcp-id"))
        assert await _resolve(headers={"host": "example.invalid"}) == ("fastmcp-id", origin)

    @requires_http_injection
    @pytest.mark.parametrize(("initialized", "origin"), [(True, "observed"), (False, "minted")])
    async def test_the_current_context_decides_the_origin(
        self, monkeypatch: pytest.MonkeyPatch, initialized: bool, origin: str
    ) -> None:
        """Tool calls and annotations pass no context, so the origin must come
        from the current one."""
        context = _FakeContext(initialized=initialized)
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", lambda: context)
        assert await _resolve(headers={"host": "x"}) == ("fastmcp-id", origin)

    async def test_not_used_without_headers_so_stdio_keeps_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stdio / in-process carry no headers. One process is one client and
        ``fallback`` already says so; firing here would mint a second
        per-process id that disagrees with the install-time
        ``surface_snapshot``'s, splitting a session from its own surface."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("a-different-uuid"))
        assert await _resolve() == ("sdk-fallback", "minted")

    @requires_http_injection
    async def test_header_outranks_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("per-connection-id"))
        assert await _resolve(headers={"mcp-session-id": "from-header"}) == (
            "from-header",
            "observed",
        )

    @requires_http_injection
    async def test_gated_off_where_the_cache_does_not_survive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On mcp 2.x fastmcp's id is minted fresh per call, so the rung must not
        fire and the ladder goes on to rung 5."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", False)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("fresh-every-call"))
        assert await _resolve(headers={"host": "x"}) == (PER_CALL, "minted")

    @requires_http_injection
    async def test_no_active_context_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Outside a request ``get_context`` raises; a correlation rung must
        never be able to fail the tool call it is describing."""

        def boom() -> Any:
            raise RuntimeError("No active context found.")

        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", boom)
        assert await _resolve(headers={"host": "x"}) == (PER_CALL, "minted")

    @requires_http_injection
    async def test_a_per_request_id_is_numbered_as_per_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stateless request's id is never seen again, so a per-session
        counter entry for it would never be freed."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext(initialized=False))
        with set_http_request(_fake_http_request({"host": "x"})):
            session = await resolve_call_session(
                headers=extract_headers(), fallback="sdk-fallback", transport_observed="http"
            )
        assert (session.id, session.per_call) == ("fastmcp-id", True)

    @requires_http_injection
    async def test_an_unreadable_session_keeps_the_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The id may be an SSE connection's, so switching it to the clock
        would send its numbers backwards on the next readable call."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "_session_scope", lambda _context: "unknown")
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("conn-id"))
        with set_http_request(_fake_http_request({"host": "x"})):
            session = await resolve_call_session(
                headers=extract_headers(), fallback="sdk-fallback", transport_observed="http"
            )
        assert (session.id, session.origin, session.per_call) == ("conn-id", "minted", False)

    async def test_gate_matches_the_installed_mcp_major(self) -> None:
        """The gate is a claim about the INSTALLED stack, so pin it to that
        rather than to a hardcoded expectation — this is the assertion that
        would fail if fastmcp and mcp ever stopped moving in lockstep."""
        major = int(version("mcp").split(".")[0])
        assert _session._session_cache_survives() is (major < 2)
