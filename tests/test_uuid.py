"""Tests for the in-tree UUIDv7 generator (``baton._uuid``).

These guard the property that previously justified the ``uuid6`` dependency:
same-millisecond monotonicity. We test the pure-Python fallback directly (rather
than the exported ``uuid7``, which is the stdlib implementation on 3.14+) so the
fallback is covered on every interpreter version.
"""

from __future__ import annotations

import time
import uuid
from itertools import pairwise

from baton._uuid import _uuid7_fallback, uuid7


def test_exported_uuid7_is_callable_and_v7() -> None:
    u = uuid7()
    assert isinstance(u, uuid.UUID)
    assert u.version == 7


def test_fallback_version_and_variant_bits() -> None:
    for _ in range(1000):
        u = _uuid7_fallback()
        assert u.version == 7
        # RFC 9562 variant is 0b10 in the two MSBs of the clock-seq octet.
        assert (u.int >> 62) & 0b11 == 0b10


def test_fallback_strictly_monotonic_and_unique() -> None:
    # A tight burst spans multiple sub-millisecond calls in the same tick;
    # the 12-bit counter must keep them strictly increasing and collision-free.
    ids = [_uuid7_fallback() for _ in range(5000)]
    assert all(a < b for a, b in pairwise(ids)), "not strictly monotonic"
    assert len(set(ids)) == len(ids), "collision within the same millisecond"


def test_fallback_timestamp_is_current() -> None:
    now_ms = time.time_ns() // 1_000_000
    ts = _uuid7_fallback().int >> 80  # top 48 bits are the unix-ms timestamp
    assert abs(now_ms - ts) < 1000
