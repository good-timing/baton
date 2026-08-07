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


class ProactiveTracker:
    """Tracks which sessions have already emitted a proactive annotation.

    Coordinates the two proactive sources so a session opens at most one:
    the ``BatonMiddleware`` synthesises a proactive from the first injected
    ``user_goal`` it sees, and the annotation tool emits one when the agent
    calls it proactively (``signal_type is None``). On instruction-aware
    runtimes both fire; on Claude Desktop only the injected-param path does.

    Both methods are synchronous and mutate a plain set — safe because all
    callers run on the one asyncio loop, so there is no true concurrency
    between the check and the mutation (no ``await`` interleaves them).
    """

    def __init__(self) -> None:
        self._emitted: set[str] = set()

    def claim(self, session_id: str) -> bool:
        """Claim the session's proactive slot. Returns True exactly once per
        session (the caller should then emit); False if already claimed."""
        if session_id in self._emitted:
            return False
        self._emitted.add(session_id)
        return True

    def mark(self, session_id: str) -> None:
        """Record that a proactive already fired for this session (e.g. a real
        annotation-tool proactive), suppressing a later synthesised one."""
        self._emitted.add(session_id)


def resolve_session_id(fastmcp_ctx: Any, fallback: str) -> str:
    """Get the session_id from FastMCP's Context, falling back to ``fallback``
    if no session info is available."""
    if fastmcp_ctx is not None:
        session_id = getattr(fastmcp_ctx, "session_id", None)
        if isinstance(session_id, str) and session_id:
            return session_id
    return fallback
