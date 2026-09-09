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
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.http import set_http_request
from starlette.requests import Request

from baton.integrations.standalone import _session
from baton.integrations.standalone._session import resolve_call_session_id


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
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"mcp-session-id", b"probe")],
    }
    with set_http_request(Request(scope)):
        return (get_http_headers(include_all=True) or {}).get("mcp-session-id") == "probe"


requires_http_injection = pytest.mark.skipif(
    not _http_injection_works(),
    reason="fastmcp's set_http_request is not observable via get_http_headers on this version",
)


def _fake_http_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


async def _resolve(
    *,
    meta: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    hook: Any = None,
    fallback: str = "sdk-fallback",
) -> str:
    async def call() -> str:
        return await resolve_call_session_id(
            meta=meta,
            fallback=fallback,
            resolve_hook=hook,
            tool_name="echo",
            arguments={"text": "x"},
        )

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
    """

    @requires_http_injection
    async def test_traceparent_no_longer_outranks_the_header(self) -> None:
        got = await _resolve(
            meta={"traceparent": TRACEPARENT}, headers={"mcp-session-id": "from-header"}
        )
        assert got == "from-header"

    async def test_traceparent_alone_leaves_the_fallback(self) -> None:
        assert await _resolve(meta={"traceparent": TRACEPARENT}) == "sdk-fallback"

    async def test_io_baton_session_id_alone_leaves_the_fallback(self) -> None:
        assert await _resolve(meta={"io.baton/session_id": "vendor-app-handle"}) == "sdk-fallback"

    @requires_http_injection
    async def test_neither_key_outranks_the_header_even_together(self) -> None:
        got = await _resolve(
            meta={"traceparent": TRACEPARENT, "io.baton/session_id": "vendor-app-handle"},
            headers={"mcp-session-id": "from-header"},
        )
        assert got == "from-header"


class TestHeaderAndFallbackRungs:
    @requires_http_injection
    async def test_header_used_when_meta_is_empty(self) -> None:
        assert await _resolve(meta=None, headers={"mcp-session-id": "hdr"}) == "hdr"

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


class TestHookRungZero:
    @requires_http_injection
    async def test_hook_wins_over_every_lower_rung(self) -> None:
        got = await _resolve(
            meta={"traceparent": TRACEPARENT},
            headers={"mcp-session-id": "from-header"},
            hook=lambda ctx: "vendor-resolved",
        )
        assert got == "vendor-resolved"

    async def test_hook_returning_none_falls_through(self) -> None:
        got = await _resolve(meta={"traceparent": TRACEPARENT}, hook=lambda ctx: None)
        assert got == "sdk-fallback"

    async def test_raising_hook_falls_through_and_does_not_propagate(self) -> None:
        def broken(ctx: Any) -> str:
            raise RuntimeError("vendor bug")

        assert await _resolve(meta={"traceparent": TRACEPARENT}, hook=broken) == "sdk-fallback"

    @requires_http_injection
    async def test_hook_sees_headers_and_meta(self) -> None:
        seen: dict[str, Any] = {}

        def capture(ctx: Any) -> None:
            seen["headers"] = dict(ctx.headers or {})
            seen["meta"] = ctx.meta
            seen["tool_name"] = ctx.tool_name
            return None

        await _resolve(meta={"k": "v"}, headers={"mcp-session-id": "hdr"}, hook=capture)
        assert seen["headers"].get("mcp-session-id") == "hdr"
        assert seen["meta"] == {"k": "v"}
        assert seen["tool_name"] == "echo"


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
    async def test_meta_no_longer_outranks_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Before the rung-1/2 retirement a ``traceparent`` beat this rung. It
        no longer does, so 4b now answers the SSE shape even when the client
        propagates a trace context — which is the intended widening, not a
        regression: 4b is an id the server library owns."""
        monkeypatch.setattr(_session, "_SESSION_CACHE_SURVIVES", True)
        monkeypatch.setattr(_session, "get_context", lambda: _FakeContext("per-connection-id"))
        got = await _resolve(meta={"traceparent": TRACEPARENT}, headers={"host": "x"})
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
