"""The forced-reorder rig's own guard, checked.

``tests/_forced_reorder.py`` exists so the two ``call_id`` pairing suites cannot
pass on a run where nothing overlapped. That guard is the same kind of code it
is defending against: V2's extractor was never exercised either, matched
nothing, and turned "not checked" into a green tick. So the guard gets a test
that feeds it streams which must NOT be accepted.

Synthetic envelopes here on purpose — the point is the guard's logic, and the
adapters exercise it against real ones in ``tests/integrations/*/``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from baton.events import (
    Event,
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallErrorEvent,
    ToolCallErrorPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from tests._forced_reorder import (
    FAST,
    SLOW,
    TOOL_NAME,
    ReorderGates,
    assert_the_rig_inverted,
    fifo_pairs,
    legs,
)


def _envelope() -> dict[str, Any]:
    return {
        "tenant_id": "ten_rig",
        "vendor_id": "ten_rig",
        "session_id": "sess_rig",
        "sequence_number": 1,
        "captured_at": datetime.now(UTC),
        "consent_token": "ct_rig",
        "agent_runtime": "claude-code",
    }


def _start(tag: str) -> Event:
    return ToolCallStartEvent(
        **_envelope(),
        payload=ToolCallStartPayload(tool_name=TOOL_NAME, params={"tag": tag}),
    )


def _end() -> Event:
    return ToolCallEndEvent(
        **_envelope(),
        payload=ToolCallEndPayload(tool_name=TOOL_NAME, result={"tag": SLOW}),
    )


def _error() -> Event:
    return ToolCallErrorEvent(
        **_envelope(),
        payload=ToolCallErrorPayload(
            tool_name=TOOL_NAME,
            error_type="FastLegFailed",
            error_body="the fast leg fails on purpose",
        ),
    )


def _inverted() -> tuple[list[Event], list[Event]]:
    """What a correct run emits: start(SLOW), start(FAST), error(FAST), end(SLOW)."""
    return legs([_start(SLOW), _start(FAST), _error(), _end()])


def test_the_guard_accepts_a_run_that_actually_inverted() -> None:
    starts, ends = _inverted()
    assert_the_rig_inverted(ReorderGates(), starts, ends)
    assert fifo_pairs(starts, ends) == [(SLOW, FAST), (FAST, SLOW)]


def test_the_guard_rejects_a_run_that_completed_in_start_order() -> None:
    """The failure that matters most: the calls ran, both legs are present, and
    FIFO would have paired them CORRECTLY — so a pairing assertion passes while
    proving nothing about the defect."""
    starts, ends = legs([_start(SLOW), _start(FAST), _end(), _error()])
    with pytest.raises(AssertionError, match="inversion"):
        assert_the_rig_inverted(ReorderGates(), starts, ends)


def test_the_guard_rejects_a_run_where_the_gate_timed_out() -> None:
    gates = ReorderGates()
    gates.timed_out = True
    starts, ends = _inverted()
    with pytest.raises(AssertionError, match="never overlapped"):
        assert_the_rig_inverted(gates, starts, ends)


def test_the_guard_rejects_a_truncated_stream() -> None:
    starts, ends = legs([_start(SLOW), _error()])
    with pytest.raises(AssertionError, match="expected 2 starts"):
        assert_the_rig_inverted(ReorderGates(), starts, ends)


def test_the_guard_rejects_calls_that_started_in_the_wrong_order() -> None:
    starts, ends = legs([_start(FAST), _start(SLOW), _error(), _end()])
    with pytest.raises(AssertionError, match="must START first"):
        assert_the_rig_inverted(ReorderGates(), starts, ends)
