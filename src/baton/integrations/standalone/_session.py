"""SPEC §3.4 session-id resolution for the standalone ``fastmcp`` adapter.

One resolver, used by BOTH capture paths — ``BatonMiddleware`` (tool calls) and
the registered annotation tool. That sharing is load-bearing rather than tidy:
an annotation whose ``session_id`` disagrees with the tool call it describes can
never be joined to it downstream, which is the single correlation the sensor
exists to produce. Two ladders would let each path's tests pass while the
product stayed broken.

**Rung 4 is read from the ``mcp-session-id`` header, not from fastmcp's
``Context.session_id``.** That property caches its generated id on the
``ServerSession`` (on 4.x, on that session's ``_connection``), and under MCP SDK
v2 *both* objects are rebuilt per request — so every call mints a fresh
``uuid4``. Verified first-hand on fastmcp 4.0.2 across in-process, stdio and
streamable HTTP: three calls on one client connection, three different ids. The
damage is silent — events ship and nothing errors, but sequence numbers restart
at 1 per call, the once-per-session proactive fires every call, and an
``*_annotate`` lands under a different id than the call it annotates.

**But the header is not carried on every transport, so it cannot be the last
rung before the fallback.** SSE never sends it: ``mcp.server.sse`` carries its id
as a ``session_id`` QUERY PARAM and the literal string ``mcp-session-id`` does
not appear in that module. Reading only the header therefore regressed SSE from
a real per-client id to the process-wide fallback — measured with two concurrent
clients on one server, which shared one id and mis-attributed 2 of 8 call pairs
(``baton-internal`` `mcp_integration_seams.md` §Validation V2). That direction is
the worse one: the bug this ladder fixes SPLIT one client into many and cost
joins; the fallback MERGES many clients into one and manufactures joins between
strangers.

So ``Context.session_id`` returns as **rung 4b**, below the header and above the
fallback, and doubly gated: to the band where its cache actually survives (see
``_session_cache_survives``), and to requests that carry HTTP headers at all.
That second gate narrows it to HTTP requests the header rung did not answer. On
STATEFUL old-spec streamable HTTP the header rung has already answered; on stdio
and in-process there are no headers, one process IS one client, and ``fallback``
already says so — firing there would only mint a second per-process id that
disagrees with the ``surface_snapshot``'s.

**Two cases are left, not one.** SSE is the one this rung exists for: a live HTTP
request, no ``mcp-session-id``, and a per-connection id that reading the header
alone throws away. The other is **stateless streamable HTTP**
(``stateless_http=True`` / ``FASTMCP_STATELESS_HTTP``), where mcp builds a fresh
transport and a fresh ``ServerSession`` per request
(``streamable_http_manager._handle_stateless_request``) and issues no
``mcp-session-id`` — so the rung fires and ``Context.session_id`` mints a fresh
``uuid4`` for every call. Measured on fastmcp 3.4.2 / mcp 1.27.2, one client,
three sequential calls: three ids with the rung on, the single process-wide
``fallback`` with it off.

**That case is knowingly not fixed here, and this rung is not what broke it.**
0.6.1 resolved solely through this same property, so a stateless deployment has
always minted per-request ids; the rung reproduces the released behaviour rather
than regressing it. It is also the survivable direction: a stateless server has
no session by protocol design, and splitting one client's calls costs joins,
where routing them to the process-wide ``fallback`` would merge every client of
a multi-user server and manufacture joins between strangers. Start/end pairing
survives either way — both legs of a call share one request, so they share the
id; what a split costs is ``sequence_number`` continuity (restarts at 1 per
call), the once-per-session proactive (fires per call), and the cross-request
``*_annotate`` → call join. SPEC §3.4's real answer here is rung 5, a per-event
UUID with ``correlation_mode=per-event``, unbuilt in both adapters and tracked
as D2.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastmcp.server.dependencies import get_context, get_http_headers

from baton.integrations._config import (
    ResolveSessionIdHook,
    SessionResolutionContext,
    resolve_via_hook,
)
from baton.integrations._session import session_id_from_headers

logger = logging.getLogger(__name__)


def _session_cache_survives() -> bool:
    """Whether fastmcp's ``Context.session_id`` holds still across calls here.

    Gated on the **mcp** major, not fastmcp's, because mcp owns the mechanism:
    ``Context.session_id`` stashes its id on ``request_context.session`` (a
    ``ServerSession``), and mcp 2.x rebuilds that object — and, on fastmcp 4,
    the ``_connection`` it moved the cache to — for every request. mcp 1.x keeps
    one per connection, so the id survives.

    The two libraries move in lockstep today (fastmcp 2.x/3.x pin ``mcp<2``;
    fastmcp 4 requires ``mcp>=2``), so either version would discriminate — this
    one names the cause, and stays right if that pairing ever changes.

    Unknown version means DO NOT use the rung: losing a join is recoverable,
    inventing one between two users is not.

    ⚠ fastmcp 4 believes it fixed this by caching on the connection, commented
    as persisting "for the whole client session". Probed on 4.0.2 in-process:
    three calls, three ids AND three different ``_connection`` objects. Do not
    re-open this on the strength of reading their source.
    """
    try:
        major = int(version("mcp").split(".")[0])
    except (PackageNotFoundError, ValueError, IndexError):  # pragma: no cover
        logger.debug("baton: mcp version undeterminable; skipping session rung 4b")
        return False
    return major < 2


_SESSION_CACHE_SURVIVES = _session_cache_survives()


def session_id_from_fastmcp_context(headers: Mapping[str, str] | None) -> str | None:
    """SPEC §3.4 rung 4b — fastmcp's own per-connection id, where it is stable.

    Fires only on an HTTP transport that did not carry ``mcp-session-id`` — in
    practice SSE, and also stateless streamable HTTP, where it degrades to a
    per-request id (see the module docstring: deliberately unfixed, unchanged
    since 0.6.1, and the safe direction of the two). **Not on stdio or
    in-process**, and that exclusion is
    load-bearing rather than an optimisation: there, one process is one client
    and ``fallback`` already says so correctly. Firing would mint a SECOND
    per-process id that disagrees with the one the install-time
    ``surface_snapshot`` carries, splitting a session's snapshot from its own
    calls — a real regression the suite caught
    (``test_middleware.py::TestSequenceNumbers``), and the reason the original
    port's "stdio returns what the fallback already is" reasoning does not hold:
    same SHAPE (per process), different VALUE.

    Returns ``None`` outside a live request, on a transport with no session, or
    on any version where the cache does not survive. Never raises: a
    correlation rung must not be able to fail a tool call.
    """
    if not _SESSION_CACHE_SURVIVES:
        return None
    if not headers:
        return None
    try:
        session_id = get_context().session_id
    except Exception:  # best-effort rung; see docstring
        return None
    return session_id or None


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
    session_resolver_hook.md``; (4) the ``mcp-session-id`` header;
    (4b) fastmcp's ``Context.session_id``, where its cache survives and the
    header was absent; else ``fallback``, the install-time process-wide id.
    Rung 3 (a future runtime-specific ``_meta`` key) isn't defined for any
    runtime yet, so it's skipped.

    ``fallback`` is **not** SPEC rung 5. Rung 5 is a per-event UUID carrying
    ``correlation_mode=per-event``; neither adapter implements it, so both
    terminate on a process-wide id that is stable but merges every client of a
    multi-user server. That gap is D2.

    **Rungs 1-2 were retired 2026-09-09** — ``_meta.traceparent``'s trace-id
    and a client-supplied ``_meta["io.baton/session_id"]`` both keyed the
    session on an identifier the SDK did not mint, which the D2 join rule
    forbids. The values still reach the console via ``runtime_meta``; only the
    capture-time join is gone. Before the shared module existed this adapter
    implemented neither rung, resolving only via fastmcp's own
    ``Context.session_id`` — the uneven ladder design note D3 recorded and
    deferred; retiring them makes both adapters even again, from the other
    end.
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
    from_header = session_id_from_headers(headers)
    if from_header is not None:
        return from_header
    from_context = session_id_from_fastmcp_context(headers)
    return from_context if from_context is not None else fallback
