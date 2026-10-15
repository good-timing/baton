"""SPEC §3.4 session-id resolution for the standalone ``fastmcp`` adapter.

One resolver for every capture path: ``BatonMiddleware``, the annotation tool
and the tool-list capture. An annotation whose ``session_id`` differs from the
call it describes can never be joined to it, so two ladders would let each
path's tests pass while the product broke.

**Rung 4 reads the ``mcp-session-id`` header, not ``Context.session_id``.**
That property caches its id on mcp's ``ServerSession`` (on fastmcp 4, on its
``_connection``), and mcp 2 rebuilds both for every request. Every call would
get a new id: sequence numbers restart, the once-per-session proactive fires
every call, and an annotation lands apart from its call.

**Rung 4b is ``Context.session_id``, below the header.** SSE never sends the
header (its id is a ``session_id`` query parameter), so reading only the header
would put every SSE client on the process-wide fallback and merge them. That is
the worse direction: a split only loses joins, a merge joins strangers. The
rung runs only on mcp 1, where the cache holds (``_session_cache_survives``),
and only on requests with HTTP headers: on stdio and in-process one process is
one client and ``fallback`` already says so.

On stateless streamable HTTP (``stateless_http=True``) the rung also runs, but
mcp builds a new ``ServerSession`` for every request, so the id is per request
and is sent as ``"minted"``. That split is the safe direction; sending those
calls to the process-wide fallback would merge every client of a multi-user
server. It costs ``sequence_number`` continuity, the once-per-session proactive
and the annotation-to-call join. SPEC §3.4's answer is rung 5, which is unbuilt
(D2).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastmcp.server.dependencies import get_context, get_http_headers, get_http_request

from baton.integrations._session import (
    ResolvedSession,
    session_id_from_headers,
)

logger = logging.getLogger(__name__)


def _session_cache_survives() -> bool:
    """Whether fastmcp's ``Context.session_id`` holds still across calls here.

    Gated on the **mcp** major, not fastmcp's, because mcp owns the mechanism:
    mcp 2 rebuilds the ``ServerSession`` the id is cached on (and, on fastmcp 4,
    its ``_connection``) for every request; mcp 1 keeps one per connection.
    ``test_gate_matches_the_installed_mcp_major`` pins it.

    Unknown version means do not use the rung: losing a join is recoverable,
    inventing one between two users is not.

    ⚠ fastmcp 4's source says its connection cache lasts "for the whole client
    session". It does not: each call gets a new ``_connection``. Do not enable
    the rung on mcp 2 on the strength of that comment.
    """
    try:
        major = int(version("mcp").split(".")[0])
    except (PackageNotFoundError, ValueError, IndexError):  # pragma: no cover
        logger.debug("baton: mcp version undeterminable; skipping session rung 4b")
        return False
    return major < 2


_SESSION_CACHE_SURVIVES = _session_cache_survives()


def session_from_fastmcp_context(
    headers: Mapping[str, str] | None, context: Any = None
) -> ResolvedSession | None:
    """SPEC §3.4 rung 4b — fastmcp's own per-connection id, where it is stable,
    with its origin.

    ``context`` is the fastmcp context to read it from, for a caller that runs
    where none is current.

    Fires only on an HTTP transport that did not carry ``mcp-session-id``: SSE,
    and stateless streamable HTTP, where the id is per request and so
    ``"minted"`` (see the module docstring). **Not on stdio or in-process**:
    there one process is one client and ``fallback`` already says so. Firing
    would mint a second per-process id that disagrees with the install-time
    ``surface_snapshot``'s, splitting a session from its own snapshot
    (``test_middleware.py::TestSequenceNumbers``).

    Returns ``None`` outside a live request, on a transport with no session, or
    on any version where the cache does not survive. Never raises: a
    correlation rung must not be able to fail a tool call.
    """
    if not _SESSION_CACHE_SURVIVES:
        return None
    if not headers:
        return None
    try:
        context = context if context is not None else get_context()
        session_id = context.session_id
    except Exception:  # best-effort rung; see docstring
        return None
    if not session_id:
        return None
    return ResolvedSession(
        session_id, "observed" if _client_keeps_the_session(context) else "minted"
    )


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
    ``"X-Forwarded-User"`` would miss. The fold happens once, in
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
    request" and "the read broke" into one answer, which is the merge
    ``Event.transport_observed`` forbids. Headers are also empty across the
    whole mcp 1.x band, while the request object tells the cases apart there.

    So this calls ``get_http_request`` itself and reads the three outcomes apart:

    - returns a request → ``"http"``
    - raises ``RuntimeError`` → ``"no-http-request"``, fastmcp's deliberate,
      named absence signal (``"No active HTTP request found."``)
    - raises anything else → ``"read-failed"``

    ⚠ **The exception type is a GUESS about a third party and is treated as one.**
    fastmcp has raised ``RuntimeError`` where its own docstring promised
    ``ValueError``. The default branch is
    therefore ``read-failed`` rather than ``no-http-request``: if a version ever
    signals absence some other way we under-report a real stdio server, which
    loses a grouping. The reverse mistake invents one.

    ``_get_http_request`` is a seam for the test that proves the last paragraph;
    nothing in the SDK passes it.
    """
    get_request = _get_http_request or get_http_request
    try:
        get_request()
    except RuntimeError:
        return "no-http-request"
    except Exception:
        logger.debug("transport_observed: the HTTP request read raised", exc_info=True)
        return "read-failed"
    return "http"


async def resolve_call_session(
    *, headers: Mapping[str, str] | None, fallback: str, context: Any = None
) -> ResolvedSession:
    """The per-call session id and its origin, SPEC §3.4's layered fallback in priority
    order: (4) the ``mcp-session-id`` header; (4b) fastmcp's
    ``Context.session_id``, where its cache survives and the header was
    absent; else ``fallback``, the install-time process-wide id. Rung 3 (a
    future runtime-specific ``_meta`` key) isn't defined for any runtime yet,
    so it's skipped.

    There is no vendor hook and no ``_meta`` rung: each would key the session
    on an id the SDK did not mint. What the vendor knows about a caller goes
    in ``principal`` (``VendorConfig.resolve_principal``) instead, and
    ``_meta`` reaches the console as ``runtime_meta``.

    ``fallback`` is **not** SPEC rung 5. Rung 5 is a per-event UUID; neither
    adapter implements it, so both terminate on a process-wide id that is
    stable but merges every client of a multi-user server. That gap is D2.
    ⚠ Rung 5 is also **not implementable yet**, and not only for want of code:
    SPEC now conditions it on ``transport_observed`` (this fallback is the
    CORRECT answer on stdio, where one process is one agent), and what a fired
    rung emits is undecided — a stream of fresh UUIDs is indistinguishable from
    the merge defect, which is why the ``correlation_mode`` field was dropped
    rather than kept. D-3 settles the shape.
    """
    from_header = session_id_from_headers(headers)
    if from_header is not None:
        return ResolvedSession(from_header, "observed")
    from_context = session_from_fastmcp_context(headers, context)
    if from_context is not None:
        return from_context
    return ResolvedSession(fallback, "minted")


def _client_keeps_the_session(context: Any) -> bool:
    """Whether rung 4b's id names a session the client keeps.

    mcp sets ``client_params`` only when a session handles ``initialize``. An
    SSE connection does, so fastmcp's id holds for the connection. Stateless
    streamable HTTP builds a new session for every request, already marked
    initialized, so it never has them and fastmcp's id is new per request
    (SPEC §11.4 ``"minted"``). A failed read answers False: the console may
    then join that connection with the same person's other sessions, never
    with a stranger's."""
    try:
        return context.request_context.session.client_params is not None
    except Exception:
        logger.debug("session_id_origin: could not read the session", exc_info=True)
        return False
