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
UUID, unbuilt in both adapters and tracked as D2. (It used to read
``correlation_mode=per-event``; that envelope field was dropped 2026-09-09 and
removed from SPEC 2026-09-15 — it could not tell a deliberate per-event stream
apart from the merge defect, since both emit a fresh UUID per event.)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError, version

from fastmcp.server.dependencies import get_context, get_http_headers

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
    strips (e.g. ``authorization``, which both the session-lookup hook and the
    ``resolve_principal`` identity hook may need).

    ⚠ **The ``dict`` this returns is case-SENSITIVE and that is deliberate.**
    ASGI has already lowercased its keys, so a vendor hook reading the canonical
    ``"X-Forwarded-User"`` would miss — register A8. The fold happens once, in
    ``SessionResolutionContext.__post_init__``, rather than here: this function
    also feeds rung 4, which looks up an already-lowercase constant, and putting
    the guarantee in the class means a future adapter inherits it instead of
    having to remember. See ``CaseInsensitiveHeaders`` for the whole story."""
    headers = get_http_headers(include_all=True)
    return headers if headers else None


def observe_transport(*, _get_http_request: Callable[[], object] | None = None) -> str | None:
    """What we observed beneath this call, for the envelope's ``transport_observed``.

    ⚠ **Deliberately NOT built on ``extract_headers`` above, which cannot answer
    this.** It delegates to fastmcp's ``get_http_headers``, whose own contract is
    "never raises ... an empty dict if there is no active HTTP request" — so it
    swallows the ``RuntimeError`` that IS the absence signal and returns the same
    empty mapping either way. Reading a transport off it would fold "no HTTP
    request" and "the read broke" into one answer, which is the precise merge
    ``Event.transport_observed`` forbids, and the defect register A6 records in
    the official adapter's equivalent helper. Headers are also the wrong carrier
    on their own merits: they are vacuously empty across the whole mcp 1.x band
    (register A7) while the request object splits correctly there.

    So this calls ``get_http_request`` itself and reads the three outcomes apart:

    - returns a request → ``"http"``
    - raises ``RuntimeError`` → ``"no-http-request"``, fastmcp's deliberate,
      named absence signal (``"No active HTTP request found."``)
    - raises anything else → ``"read-failed"``

    ⚠ **The exception type is a GUESS about a third party and is treated as one.**
    ``RuntimeError`` is confirmed on fastmcp 3.3.1 / 3.4.7 / 4.0.2 / 4.0.3, and
    commit ``8b4356d`` in this repo records fastmcp raising ``RuntimeError``
    where its own docstring promised ``ValueError``. The default branch is
    therefore ``read-failed`` rather than ``no-http-request``: if a version ever
    signals absence some other way we under-report a real stdio server, which
    loses a grouping. The reverse mistake invents one.

    ``_get_http_request`` is a seam for the test that proves the last paragraph;
    nothing in the SDK passes it.
    """
    get_request = _get_http_request
    if get_request is None:
        from fastmcp.server.dependencies import get_http_request

        get_request = get_http_request
    try:
        get_request()
    except RuntimeError:
        return "no-http-request"
    except Exception:
        logger.debug("transport_observed: the HTTP request read raised", exc_info=True)
        return "read-failed"
    return "http"


async def resolve_call_session_id(*, fallback: str) -> str:
    """Real per-call session id, SPEC §3.4's layered fallback in priority
    order: (4) the ``mcp-session-id`` header; (4b) fastmcp's
    ``Context.session_id``, where its cache survives and the header was
    absent; else ``fallback``, the install-time process-wide id. Rung 3 (a
    future runtime-specific ``_meta`` key) isn't defined for any runtime yet,
    so it's skipped.

    **Rung 0 — ``VendorConfig.resolve_session_id`` — was REMOVED 2026-09-12**,
    for the reason that retired rungs 1-2: it keyed the session on an
    identifier the SDK did not mint. It differed from those two only in who
    supplied the value, and the join rule does not distinguish a client's
    handle from a vendor's. What the vendor knows about a caller now reaches
    Baton through ``VendorConfig.resolve_principal``, which lands in ``principal_id`` —
    a field the console can partition on downstream, where the decision can
    be changed and re-run.

    ``fallback`` is **not** SPEC rung 5. Rung 5 is a per-event UUID; neither
    adapter implements it, so both terminate on a process-wide id that is
    stable but merges every client of a multi-user server. That gap is D2.
    ⚠ Rung 5 is also **not implementable yet**, and not only for want of code:
    SPEC now conditions it on ``transport_observed`` (this fallback is the
    CORRECT answer on stdio, where one process is one agent), and what a fired
    rung emits is undecided — a stream of fresh UUIDs is indistinguishable from
    the merge defect, which is why the ``correlation_mode`` field was dropped
    rather than kept. D-3 settles the shape.

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
    from_header = session_id_from_headers(headers)
    if from_header is not None:
        return from_header
    from_context = session_id_from_fastmcp_context(headers)
    return from_context if from_context is not None else fallback
