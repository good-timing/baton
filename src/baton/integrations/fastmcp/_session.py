"""SPEC §3.4 session-id resolution for the standalone ``fastmcp`` adapter.

One resolver, used by BOTH capture paths — ``BatonMiddleware`` (tool calls) and
the registered annotation tool. That sharing is load-bearing rather than tidy:
an annotation whose ``session_id`` disagrees with the tool call it describes can
never be joined to it downstream, which is the single correlation the sensor
exists to produce. Two ladders would let each path's tests pass while the
product stayed broken.

**Why this does not use fastmcp's own ``Context.session_id``.** It is SPEC §3.4
rung 4, and on fastmcp 4.x it does not hold: ``Context.session_id`` caches its
generated id on the ``ServerSession`` (or, on 4.x, that session's
``_connection``), and under MCP SDK v2 *both* of those objects are rebuilt per
request — so every call mints a fresh ``uuid4``. Verified first-hand on
fastmcp 4.0.2 across all three transports (in-process, stdio, streamable HTTP):
three calls on one client connection, three different ids. The damage is silent
— events ship and nothing errors, but sequence numbers restart at 1 per call,
the once-per-session proactive fires on every call, and an ``*_annotate`` lands
under a different id than the call it annotates.

So rung 4 is read from its actual carrier, the ``mcp-session-id`` header, the
same way the official-SDK adapter reads it. This is behaviour-preserving on
fastmcp 2.x/3.x rather than a downgrade: on old-spec streamable HTTP
``Context.session_id`` returns that very header, and on stdio it returns a
per-process ``uuid4`` — which is what ``fallback`` already is, and one stdio
process is one client either way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastmcp.server.dependencies import get_http_headers

from baton.integrations._config import (
    ResolveSessionIdHook,
    SessionResolutionContext,
    resolve_via_hook,
)
from baton.integrations._session import resolve_session_id_from_meta, session_id_from_headers


def extract_headers() -> Mapping[str, str] | None:
    """Best-effort HTTP header extraction via FastMCP's context-var-backed
    ``get_http_headers`` (set by ``RequestContextMiddleware`` around the whole
    request, so it's populated by the time either capture path runs). Never
    raises — empty outside a live HTTP request (e.g. stdio).
    ``include_all=True`` so a vendor's hook can read headers the default view
    strips (e.g. ``authorization``, which a session-lookup hook may need)."""
    headers = get_http_headers(include_all=True)
    return headers if headers else None


async def resolve_call_session_id(
    *,
    meta: dict[str, Any] | None,
    fallback: str,
    resolve_hook: ResolveSessionIdHook | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Real per-call session id, SPEC §3.4's layered fallback in priority
    order: (0) a configured ``VendorConfig.resolve_session_id`` hook, which on
    a non-empty return wins outright — see ``docs/design-notes/
    session_resolver_hook.md``; (1) ``_meta.traceparent``; (2)
    ``_meta["io.baton/session_id"]``; (4) the ``mcp-session-id`` header; else
    (5) ``fallback``, the install-time process-wide id. Rung 3 (a future
    runtime-specific ``_meta`` key) isn't defined for any runtime yet, so it's
    skipped.

    Rungs 1-2 are shared with the official-SDK adapter via
    ``integrations._session``; before this existed, this adapter implemented
    neither, resolving only via fastmcp's own ``Context.session_id`` — the
    uneven ladder that design note D3 recorded and deferred.
    """
    headers = extract_headers()
    if resolve_hook is not None:
        hook_result = await resolve_via_hook(
            resolve_hook,
            SessionResolutionContext(
                headers=headers, meta=meta, tool_name=tool_name, arguments=arguments
            ),
        )
        if hook_result is not None:
            return hook_result
    from_meta = resolve_session_id_from_meta(meta)
    if from_meta is not None:
        return from_meta
    from_header = session_id_from_headers(headers)
    return from_header if from_header is not None else fallback
