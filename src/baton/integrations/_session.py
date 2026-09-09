"""Shared SPEC §3.4 session-id resolution — both adapters
(``baton.integrations.standalone``, ``baton.integrations.official``) climb the same
ladder, so this module owns the rungs that are transport-independent and they
cannot drift.

**Rungs 1-2 were RETIRED on 2026-09-09 and this module is what is left.** Rung 1
took the trace-id out of ``_meta.traceparent`` and used it AS the ``session_id``;
rung 2 did the same with a client-supplied ``_meta["io.baton/session_id"]``. Both
keyed on an identifier the SDK did not mint, which the D2 join rule forbids: the
SDK mints a ``call_id`` and everything else — ``traceparent``, the MCP session id,
``clientInfo``, the JSON-RPC request id — is emitted as data and keyed on by
nothing at capture time. OTel forbids rung 1 independently, because a trace spans
one TURN rather than one conversation, so using it as a conversation id
over-fragments systematically.

⚠ **Retired means "not keyed on", NOT "not captured".** Both values still reach
the console: ``runtime_meta`` forwards the whole ``_meta`` dict unchanged, so a
vendor-supplied handle or a trace context can still be grouped on DOWNSTREAM,
where the decision can be changed and re-run against stored events. Do not
"restore" these rungs to recover data that was never lost.

Rung 0 (the vendor hook) is owned by ``_config``; rung 4 is transport-specific
and stays with each adapter, because "the MCP protocol-level session" is reached
differently on each (a raw request object on the official SDK, a
context-var-backed header view on the standalone ``fastmcp`` library).
"""

from __future__ import annotations

from typing import Any

#: Rung 4's carrier on both adapters. Present on old-spec streamable HTTP;
#: removed from the wire entirely by MCP 2026-07-28+ (SEP-2567), and never
#: present on stdio.
MCP_SESSION_ID_HEADER = "mcp-session-id"


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
