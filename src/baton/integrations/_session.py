"""Shared SPEC §3.4 session-id resolution: the parts both adapters do the
same way, so they cannot drift. Each adapter reaches the request's headers its
own way and passes them to ``session_id_from_headers``.

There is no ``_meta`` rung and no vendor hook. ``traceparent``'s trace id, a
client-supplied ``_meta["io.baton/session_id"]`` or a vendor's own handle would
key the session on an id the SDK did not mint, which the D2 join rule forbids;
a trace also spans one turn, not one conversation. ``_meta`` still reaches the
console whole as ``runtime_meta``, where it can be grouped on downstream, so
do not add these back as rungs to "recover" it.
"""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

#: Rung 4's carrier on both adapters. Present on old-spec streamable HTTP;
#: removed from the wire entirely by MCP 2026-07-28+ (SEP-2567), and never
#: present on stdio.
MCP_SESSION_ID_HEADER = "mcp-session-id"

SessionOrigin = Literal["observed", "minted"]


class ResolvedSession(NamedTuple):
    """A ``session_id`` with the envelope's ``session_id_origin`` (SPEC §11.4).
    How the id was found decides the origin, never the id's spelling."""

    id: str
    origin: SessionOrigin


def session_id_from_headers(headers: Any) -> str | None:
    """SPEC §3.4 rung 4 — the MCP protocol-level session, carried as the
    ``mcp-session-id`` HTTP header. ``None`` on stdio, on stateless HTTP, and on
    new-spec (SEP-2567) streamable HTTP, where the header is absent by design.
    Never raises.

    Stripped, and blank is a miss: ``session_id`` is the primary grouping key,
    so a whitespace-only value would put every such call in one session and
    merge strangers, while ``" abc "`` and ``"abc"`` would split one client
    (``test_hook_containment.py``). RFC 9110 §5.5 makes surrounding whitespace
    no part of a field value, and the mapping here is whatever the transport
    hands over, so it is stripped where it is read.
    """
    if not headers:
        return None
    try:
        value = headers.get(MCP_SESSION_ID_HEADER)
    except (AttributeError, TypeError):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
