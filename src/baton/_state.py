"""Internal state primitives shared between middleware and annotation tool.

A per-session sequence-number counter that both ``BatonMiddleware`` (for
``tool_call_*`` events) and the registered annotation tool handler (for
``annotation`` events) use, so sequence numbers stay monotonic within a
session regardless of which path emitted the event.

Session-id resolution is NOT here: it is SPEC §3.4's layered ladder, and it
lives with the adapters (``integrations._session`` for the rungs both share,
``integrations.standalone._session`` / ``integrations.official._tool_wrap`` for the
transport-specific ones).
"""

from __future__ import annotations

import asyncio
import threading
import time


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


class SequenceClock:
    """Sequence numbers from the clock, in microseconds (SPEC §5.6 rule 3).

    One counter for the process (``PROCESS_SEQUENCE``), shared by every
    client: strictly increasing across all sessions is strictly increasing
    within each, and nothing grows per session.
    """

    def __init__(self) -> None:
        self._last = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._last = max(self._last + 1, time.time_ns() // 1000)
            return self._last


PROCESS_SEQUENCE = SequenceClock()
