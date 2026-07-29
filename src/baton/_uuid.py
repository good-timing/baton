"""UUIDv7 generation with zero third-party dependencies.

Every emitted event's ``event_id`` and every fallback ``session_id`` is a
UUIDv7 (RFC 9562 §5.7) — time-ordered, so events sort by creation without a
separate timestamp index. The stdlib gained ``uuid.uuid7`` in 3.14; we support
3.11+, so below that we ship a monotonic fallback here rather than take a
runtime dependency (the SDK is dropped into third-party MCP servers via PR, so
every avoided dep is one the vendor doesn't inherit).

The fallback preserves **same-millisecond monotonicity** — the property that
made a naive polyfill insufficient and previously justified the ``uuid6``
dependency. Within one millisecond tick, a 12-bit counter in ``rand_a`` is
incremented per call (seeded with 11 random bits + one guard bit of headroom so
increments don't overflow), guaranteeing strictly increasing ids; ``rand_b``
carries 62 fresh random bits for uniqueness. Across ticks the millisecond field
dominates the ordering. Thread-safe via a module lock.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

__all__ = ["uuid7"]

_stdlib_uuid7 = getattr(uuid, "uuid7", None)

_lock = threading.Lock()
_last_ms = -1
_counter = 0

_MS_MASK = (1 << 48) - 1
_COUNTER_MASK = (1 << 12) - 1
_RAND_B_MASK = (1 << 62) - 1


def _uuid7_fallback() -> uuid.UUID:
    """RFC 9562 v7 UUID with per-millisecond monotonic counter (Python < 3.14)."""
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms == _last_ms:
            _counter += 1
        else:
            _last_ms = ms
            # 11-bit random seed leaves a guard bit so intra-ms increments have
            # ~2048 of headroom before the 12-bit counter field overflows.
            _counter = int.from_bytes(os.urandom(2), "big") & 0x07FF
        counter = _counter & _COUNTER_MASK
        rand_b = int.from_bytes(os.urandom(8), "big") & _RAND_B_MASK

    value = (ms & _MS_MASK) << 80
    value |= 0x7 << 76  # version
    value |= counter << 64  # rand_a: monotonic counter
    value |= 0b10 << 62  # variant (RFC 9562)
    value |= rand_b  # rand_b: 62 random bits
    return uuid.UUID(int=value)


# Prefer the stdlib implementation on 3.14+; it is monotonic per the RFC too.
uuid7 = _stdlib_uuid7 if _stdlib_uuid7 is not None else _uuid7_fallback
