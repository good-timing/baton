"""A forced start/end REORDER on ONE session — the rig both adapters' pairing
tests drive.

Two calls to the SAME tool, on one session, where the one that started FIRST
finishes LAST. That is the shape FIFO pairing gets wrong: it keys on
``(session_id, tool_name)`` and hands each end to the oldest pending start, so
an inverted completion order attributes one call's outcome to the other call's
arguments. Here the inverted call is also the FAILING one, which is what makes
the defect legible: under FIFO the call that succeeded is recorded as having
raised, and the call that raised is recorded as having returned. Every total —
starts, ends, errors, durations — is identical either way, because a mispair is
a permutation.

**The inversion is gated, not timed.** ``SLOW`` parks on an ``asyncio.Event``
that ``FAST`` sets on its way out, so the order is a happens-before, not a race
between two sleeps. V5's lesson from the concurrent-session work was that an
overlap which is not FORCED is not an overlap; the same holds for an inversion.
A sleep-based rig passes on a machine that happens to schedule the two calls in
the other order — exactly when the bug it exists to catch is invisible.

**Ground truth is the EVENT TYPE, not the payload and never the order.** The
question under test is which end belongs to which start, so position cannot be
the answer, and the obvious alternative — echoing a token in the result — is
not portable: measured 2026-09-09, ``fastmcp 2.14.7`` (the floor leg of
``fastmcp-matrix``) emits ``tool_call_end.result`` as an opaque
``<fastmcp.tools.tool.ToolResult object at 0x…>`` repr with ``duration_ms: 0``,
so neither the result body nor the duration can identify a leg there. Making
one call raise sidesteps the whole problem: ``tool_call_error`` is the failing
call's leg on every version of both libraries, and it makes the error path part
of what the mint has to thread rather than an untested branch.

Measured before these tests were written: the inversion reproduces in-process,
on one session, on **mcp 1.20.0 / 1.25.0 / 1.27.2 / 2.0.0** and on **fastmcp
2.14.7 / 3.4.2 / 4.0.2** — every leg of both CI matrices. No supported version
serialises requests per session, so no leg needs a skip.
"""

from __future__ import annotations

import asyncio

from baton.events import Event
from baton.sinks import Sink

#: The two calls. One tool, so ``tool_name`` cannot separate them — which is
#: also why FIFO has nothing left to key on.
SLOW = "slow"
FAST = "fast"

TOOL_NAME = "work"

#: Bound on every gate wait. A version that serialised requests per session
#: would deadlock here; the timeout turns that into a rig failure with a
#: message instead of a hung suite.
GATE_TIMEOUT_S = 5.0


class FastLegFailed(RuntimeError):
    """Raised by the FAST call, on purpose. Its ``tool_call_error`` is how the
    assertions tell the two calls' end legs apart."""


class CapturingSink(Sink):
    """Keeps envelopes in memory. The assertions read ``call_id`` off the
    envelope object, so nothing is serialised on the way through."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def write(self, event: Event) -> None:
        self.events.append(event)

    async def flush(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class ReorderGates:
    """Makes SLOW finish after FAST, deterministically and without sleeping."""

    def __init__(self) -> None:
        self.slow_entered = asyncio.Event()
        self.fast_finished = asyncio.Event()
        #: Set when SLOW gave up waiting. Carried here rather than in the
        #: result, because the floor fastmcp does not put the result on the
        #: wire in a readable shape (see the module docstring).
        self.timed_out = False

    async def tool_body(self, tag: str) -> dict[str, str]:
        """The vendor tool both adapters register.

        SLOW announces that it is inside the handler and parks until FAST has
        returned; FAST releases it on the way out and then raises. So the
        emission order is start(SLOW), start(FAST), error(FAST), end(SLOW) by
        construction.
        """
        if tag == SLOW:
            self.slow_entered.set()
            try:
                await asyncio.wait_for(self.fast_finished.wait(), GATE_TIMEOUT_S)
            except TimeoutError:
                # ``asyncio.TimeoutError`` is an alias of the builtin from 3.11,
                # and the package floor is 3.11 — one name, not a guess about
                # which one this loop raises.
                self.timed_out = True
            return {"tag": SLOW}
        self.fast_finished.set()
        raise FastLegFailed("the fast leg fails on purpose")

    async def wait_until_slow_is_inside(self) -> None:
        """Held by the test between launching SLOW and launching FAST, so the
        start order is a fact rather than a hope. A timeout here propagates as
        ``TimeoutError`` — a broken rig must not arrive as an ``AssertionError``
        that a strict ``xfail`` would then swallow as expected."""
        await asyncio.wait_for(self.slow_entered.wait(), GATE_TIMEOUT_S)


def legs(events: list[Event]) -> tuple[list[Event], list[Event]]:
    """The tool's start and end/error legs, in emission order."""
    starts = [
        e
        for e in events
        if e.event_type == "tool_call_start" and getattr(e.payload, "tool_name", None) == TOOL_NAME
    ]
    ends = [
        e
        for e in events
        if e.event_type in ("tool_call_end", "tool_call_error")
        and getattr(e.payload, "tool_name", None) == TOOL_NAME
    ]
    return starts, ends


def tag_of_start(event: Event) -> str | None:
    params = getattr(event.payload, "params", None)
    if not isinstance(params, dict):
        return None
    tag = params.get("tag")
    return tag if isinstance(tag, str) else None


def tag_of_end(event: Event) -> str:
    """Which call this end leg belongs to. FAST raised; SLOW returned."""
    return FAST if event.event_type == "tool_call_error" else SLOW


def assert_the_rig_inverted(gates: ReorderGates, starts: list[Event], ends: list[Event]) -> None:
    """Fail loudly when the run proved nothing.

    V2's first pass reported zero mispairs and was wrong: its extractor matched
    nothing, so every pair defaulted to correct and the green tick meant "not
    checked". Everything here exists so that cannot recur — a rig that did not
    invert must fail on its own terms rather than quietly satisfy a pairing
    assertion it never exercised.
    """
    assert not gates.timed_out, (
        "the slow call gave up waiting, so the two calls never overlapped — "
        "this run exercises neither FIFO's defect nor the fix, and any pairing "
        "assertion below would be vacuous"
    )
    assert len(starts) == 2, f"expected 2 starts for {TOOL_NAME!r}, got {len(starts)}"
    assert len(ends) == 2, f"expected 2 ends for {TOOL_NAME!r}, got {len(ends)}"

    start_tags = [tag_of_start(e) for e in starts]
    assert start_tags == [SLOW, FAST], f"the slow call must START first; saw {start_tags}"

    end_types = [e.event_type for e in ends]
    assert end_types == ["tool_call_error", "tool_call_end"], (
        "the failing (fast) call must FINISH first — that inversion is the "
        f"whole point of the rig; saw {end_types}"
    )


def fifo_pairs(starts: list[Event], ends: list[Event]) -> list[tuple[str | None, str]]:
    """What FIFO-within-``(session, tool_name)`` produces: the oldest pending
    start answered by the next end to arrive."""
    return [(tag_of_start(s), tag_of_end(e)) for s, e in zip(starts, ends, strict=True)]


def call_id_of(event: Event) -> str | None:
    """``getattr``, not attribute access, on purpose: until the mint lands the
    field does not exist on the envelope and ``event.call_id`` would raise
    ``AttributeError``. These are strict ``xfail``s that must red on their
    ASSERTION — "the mint shipped broken" and "there is no field yet" are
    different failures, and only one of them is news."""
    return getattr(event, "call_id", None)
