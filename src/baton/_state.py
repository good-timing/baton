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
import logging
import os
import threading
import time
from collections import Counter
from typing import Literal

from baton._uuid import uuid7


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


#: Why an event was not delivered. Wire values (SPEC §11.2 rule 8), so they
#: change only with SPEC.
DropReason = Literal[
    "buffer_full", "refused", "encode_failed", "write_failed", "after_close", "unsent_at_close"
]


#: The least time between two "delivering again" warnings, so steady loss
#: beside steady delivery, such as an overflowing buffer, is not one per event.
_RECOVERY_LOG_SECONDS = 60.0


class DropCounts:
    """Events this process did not deliver, by reason: every event is either
    delivered or counted here. One per process, like ``PROCESS_SEQUENCE``:
    drops happen on the vendor's threads and the bridge's."""

    def __init__(self) -> None:
        self.instance_id = uuid7()
        """Names this process in its loss reports (SPEC §11.4.7)."""
        self._total: Counter[DropReason] = Counter()
        self._since_delivery: Counter[DropReason] = Counter()
        self._last_recovery_log = -_RECOVERY_LOG_SECONDS
        self._lock = threading.Lock()

    def add(self, reason: DropReason, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self._total[reason] += n
            self._since_delivery[reason] += n

    def total(self) -> dict[DropReason, int]:
        with self._lock:
            return dict(self._total)

    def log_recovery(self) -> None:
        """Called on each delivery: names what was lost before it, the other
        half of a drop's own warning, which says only that loss started."""
        if not self._since_delivery:
            return
        now = time.monotonic()
        with self._lock:
            if now - self._last_recovery_log < _RECOVERY_LOG_SECONDS:
                return
            lost = dict(self._since_delivery)
            self._since_delivery.clear()
            self._last_recovery_log = now
        logging.getLogger("baton").warning(
            "baton: delivering again; %d event(s) lost before this: %s.",
            sum(lost.values()),
            ", ".join(f"{reason} {n}" for reason, n in sorted(lost.items())),
        )

    def _reset(self) -> None:
        self.instance_id = uuid7()
        with self._lock:
            self._total.clear()
            self._since_delivery.clear()
            self._last_recovery_log = -_RECOVERY_LOG_SECONDS


PROCESS_DROPS = DropCounts()


def _after_fork_in_child() -> None:
    # A forked child is a new process: its own id and counts. The parent's
    # locks may have been held by another thread at fork, so they are replaced.
    PROCESS_SEQUENCE._lock = threading.Lock()
    PROCESS_DROPS._lock = threading.Lock()
    PROCESS_DROPS._reset()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)
