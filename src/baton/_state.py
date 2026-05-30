"""Internal state primitives shared between middleware and annotation tool.

A per-session sequence-number counter that both ``BatonMiddleware`` (for
``tool_call_*`` events) and the registered annotation tool handler (for
``annotation`` events) use, so sequence numbers stay monotonic within a
session regardless of which path emitted the event.

Plus a session-id resolver that walks FastMCP's ``Context`` and falls back
to a process-wide UUID if no session info is available (e.g., during
in-process Client testing or stdio-without-session-tracking).
"""

from __future__ import annotations

import asyncio
from typing import Any


class SessionCounter:
    """Atomic per-session sequence-number counter.

    Use ``await counter.next(session_id)`` to get the next sequence number
    for a given session. Counters are independent across sessions; first
    call returns 1.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def next(self, session_id: str) -> int:
        async with self._lock:
            current = self._counters.get(session_id, 0)
            new = current + 1
            self._counters[session_id] = new
            return new


def resolve_session_id(fastmcp_ctx: Any, fallback: str) -> str:
    """Get the session_id from FastMCP's Context, falling back to ``fallback``
    if no session info is available."""
    if fastmcp_ctx is not None:
        session_id = getattr(fastmcp_ctx, "session_id", None)
        if isinstance(session_id, str) and session_id:
            return session_id
    return fallback
