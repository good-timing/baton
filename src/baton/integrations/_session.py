"""Shared SPEC §3.4 session-id resolution — both adapters
(``baton.integrations.fastmcp``, ``baton.integrations.mcp``) climb the same
ladder, so this module owns the rungs that are transport-independent and they
cannot drift.

Rungs 1-2 (``_meta.traceparent`` → ``_meta["io.baton/session_id"]``) live here
because they read the wire ``_meta`` both adapters already extract for
``runtime_meta``. Rung 0 (the vendor hook) is owned by ``_config``; rung 4 is
transport-specific and stays with each adapter, because "the MCP protocol-level
session" is reached differently on each (a raw request object on the official
SDK, a context-var-backed header view on the standalone ``fastmcp`` library).
"""

from __future__ import annotations

from typing import Any

#: Rung 4's carrier on both adapters. Present on old-spec streamable HTTP;
#: removed from the wire entirely by MCP 2026-07-28+ (SEP-2567), and never
#: present on stdio.
MCP_SESSION_ID_HEADER = "mcp-session-id"


def trace_id_from_traceparent(traceparent: Any) -> str | None:
    """The trace-id field of a W3C ``traceparent`` value
    (``version-trace_id-parent_id-flags``, SEP-414) — SPEC §3.4 rung 1's
    preferred ``session_id`` source. ``None`` on any malformed or all-zero
    input; never raises."""
    if not isinstance(traceparent, str):
        return None
    parts = traceparent.split("-")
    if len(parts) != 4:
        return None
    trace_id = parts[1]
    if not trace_id or trace_id == "0" * len(trace_id):
        return None
    return trace_id


def resolve_session_id_from_meta(meta: dict[str, Any] | None) -> str | None:
    """SPEC §3.4 rungs 1-2, in priority order: ``_meta.traceparent`` (W3C
    trace context, SEP-414) then ``_meta["io.baton/session_id"]``
    (vendor-supplied app-level handle). ``None`` if neither is present.

    Per SPEC §5.2's validated runtime table, no MCP client Baton has tested
    (Claude Code, Claude Desktop, Cursor) populates either key today — so in
    practice this misses for every currently-known runtime. Still worth
    reading: the data is already extracted for ``runtime_meta`` (free), and
    unlike the header rung, neither key depends on which MCP protocol version
    was negotiated — this starts resolving automatically the moment any
    runtime adopts SEP-414 or a vendor's own first-party client stamps the
    Baton key, with no further SDK change.
    """
    if not meta:
        return None
    trace_id = trace_id_from_traceparent(meta.get("traceparent"))
    if trace_id is not None:
        return trace_id
    app_handle = meta.get("io.baton/session_id")
    if isinstance(app_handle, str) and app_handle:
        return app_handle
    return None


def session_id_from_headers(headers: Any) -> str | None:
    """SPEC §3.4 rung 4 — the MCP protocol-level session, carried as the
    ``mcp-session-id`` HTTP header. ``None`` on stdio (no HTTP request), on
    stateless HTTP, and on new-spec (SEP-2567) streamable HTTP, where the
    header is absent by protocol design rather than by accident. Never raises.
    """
    if not headers:
        return None
    try:
        value = headers.get(MCP_SESSION_ID_HEADER)
    except (AttributeError, TypeError):
        return None
    return value if isinstance(value, str) and value else None
