"""SPEC §3.4 session-id ladder — standalone ``fastmcp`` adapter.

Parity coverage for the mcp adapter's ``TestSessionIdMetaRungs``. Before this
adapter grew a ladder it resolved only via fastmcp's ``Context.session_id``,
which SPEC calls rung 4 and which fastmcp 4.x mints fresh per request — see
``baton.integrations.fastmcp._session``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp.server.http import set_http_request
from starlette.requests import Request

from baton.integrations.fastmcp._session import resolve_call_session_id

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


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


class TestMetaRungs:
    """Rungs 1-2 — ``_meta.traceparent`` (SEP-414) then
    ``_meta["io.baton/session_id"]``, both ahead of the header rung."""

    async def test_traceparent_trace_id_used_as_session_id(self) -> None:
        assert await _resolve(meta={"traceparent": TRACEPARENT}) == TRACE_ID

    async def test_traceparent_takes_priority_over_header(self) -> None:
        got = await _resolve(
            meta={"traceparent": TRACEPARENT}, headers={"mcp-session-id": "from-header"}
        )
        assert got == TRACE_ID

    @pytest.mark.parametrize(
        "traceparent",
        [
            "bogus",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",  # too few fields
            "00-" + "0" * 32 + "-00f067aa0ba902b7-01",  # all-zero trace id
            None,
            42,
        ],
    )
    async def test_unusable_traceparent_falls_through_to_header(self, traceparent: Any) -> None:
        got = await _resolve(
            meta={"traceparent": traceparent}, headers={"mcp-session-id": "from-header"}
        )
        assert got == "from-header"

    async def test_io_baton_session_id_used_when_no_traceparent(self) -> None:
        assert await _resolve(meta={"io.baton/session_id": "vendor-app-handle"}) == (
            "vendor-app-handle"
        )

    async def test_io_baton_session_id_lower_priority_than_traceparent(self) -> None:
        got = await _resolve(
            meta={"traceparent": TRACEPARENT, "io.baton/session_id": "vendor-app-handle"}
        )
        assert got == TRACE_ID


class TestHeaderAndFallbackRungs:
    async def test_header_used_when_meta_is_empty(self) -> None:
        assert await _resolve(meta=None, headers={"mcp-session-id": "hdr"}) == "hdr"

    async def test_fallback_when_nothing_is_observable(self) -> None:
        """The stdio shape: no HTTP request, no meta. One process is one
        client, so the process-wide fallback is the right answer here."""
        assert await _resolve() == "sdk-fallback"

    async def test_empty_header_value_falls_through_to_fallback(self) -> None:
        assert await _resolve(headers={"mcp-session-id": ""}) == "sdk-fallback"

    async def test_unrelated_headers_do_not_resolve(self) -> None:
        assert await _resolve(headers={"x-request-id": "nope"}) == "sdk-fallback"


class TestHookRungZero:
    async def test_hook_wins_over_every_lower_rung(self) -> None:
        got = await _resolve(
            meta={"traceparent": TRACEPARENT},
            headers={"mcp-session-id": "from-header"},
            hook=lambda ctx: "vendor-resolved",
        )
        assert got == "vendor-resolved"

    async def test_hook_returning_none_falls_through(self) -> None:
        assert await _resolve(meta={"traceparent": TRACEPARENT}, hook=lambda ctx: None) == TRACE_ID

    async def test_raising_hook_falls_through_and_does_not_propagate(self) -> None:
        def broken(ctx: Any) -> str:
            raise RuntimeError("vendor bug")

        assert await _resolve(meta={"traceparent": TRACEPARENT}, hook=broken) == TRACE_ID

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
