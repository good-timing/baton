"""``call_id`` must pair a call's legs on the LIBRARY path too.

The third emit path. ``tests/integrations/{official,standalone}/test_call_id_pairing.py``
pin the two MCP adapters; ``Trace`` / ``AsyncTrace`` emit the same three event
types onto the same envelope, from a vendor's own code rather than from a tool
handler, and nothing else in the suite would notice the field missing here:
``tests/functional/envelope_assertions.py`` derives its required-field set from
the NON-nullable envelope fields, and SPEC §11.4 makes ``call_id`` optional and
nullable by design. So a mint that lands in both adapters and not here goes
green everywhere while library-API vendors keep pairing on the FIFO floor —
which is the same shape as the ``agent_runtime`` gap that survived a rename and
a release because each surface's suite asserted only about itself. Written as
strict ``xfail``s before the mint and un-marked when it landed.

The rig differs from the adapters' in one way worth stating: a ``Trace``
brackets the call in the VENDOR's code, so the two legs are emitted by
``__aenter__`` and ``__aexit__`` rather than by one function. Per-call scope is
therefore the Trace object itself, and "mint in a local variable inside the
function that emits both legs" (SPEC §11.4) has to be read as "mint per Trace"
on this path. The shipped mint resolves that as **per ENTRY** — ``__aenter__``
assigns ``_call_id``, ``__init__`` only declares it — so a Trace entered a second
time gets a second id rather than reusing the first call's. This file drives two
separate instances, so that particular property is covered by the mutation run
rather than by these assertions.
"""

from __future__ import annotations

import asyncio

import pytest

from baton import AsyncClient
from baton.events import Event
from tests._forced_reorder import (
    FAST,
    SLOW,
    TOOL_NAME,
    CapturingSink,
    FastLegFailed,
    ReorderGates,
    assert_the_rig_inverted,
    call_id_of,
    fifo_pairs,
    legs,
    tag_of_end,
    tag_of_start,
)


async def _drive() -> tuple[ReorderGates, list[Event], list[Event]]:
    """Two overlapping traces of one tool on one client, the slow one first."""
    gates = ReorderGates()
    sink = CapturingSink()
    client = AsyncClient(
        vendor_id="pairing",
        consent_token="ct_pairing",
        sink=sink,
        tenant_id="tenant-pairing",
    )

    async def traced(tag: str) -> None:
        async with client.trace(tool_name=TOOL_NAME, params={"tag": tag}) as trace:
            result = await gates.tool_body(tag)
            trace.observed(result)

    try:
        slow = asyncio.create_task(traced(SLOW))
        # Same gate as the adapter rigs: FAST is not launched until SLOW is
        # demonstrably inside its trace, so the start ORDER is a fact.
        await gates.wait_until_slow_is_inside()
        fast = asyncio.create_task(traced(FAST))
        await asyncio.gather(slow, fast, return_exceptions=True)
    finally:
        await client.aclose()

    starts, ends = legs(sink.events)
    return gates, starts, ends


async def test_the_rig_inverts_and_fifo_mispairs_because_of_it() -> None:
    """The green half: prove the run exercised the defect. Passes today and
    must keep passing after the mint — it asserts about the emitted stream,
    which the mint does not change."""
    gates, starts, ends = await _drive()
    assert_the_rig_inverted(gates, starts, ends)

    pairs = fifo_pairs(starts, ends)
    assert pairs == [(SLOW, FAST), (FAST, SLOW)], (
        f"FIFO should mispair both legs on this stream, got {pairs}"
    )


async def test_the_failing_trace_propagates_its_exception() -> None:
    """Capture must not swallow the vendor's error (SPEC §11.2). Asserted here
    because the rig depends on the FAST leg raising THROUGH the trace — if
    ``__aexit__`` ever suppressed it, the error event would still be emitted and
    the pairing tests would keep passing while the vendor's call silently
    changed behaviour."""
    with pytest.raises(FastLegFailed):
        async with AsyncClient(
            vendor_id="pairing", consent_token="ct", sink=CapturingSink()
        ) as client:
            async with client.trace(tool_name=TOOL_NAME, params={"tag": FAST}):
                raise FastLegFailed("the fast leg fails on purpose")


async def test_every_leg_of_a_traced_call_carries_a_call_id() -> None:
    _, starts, ends = await _drive()
    missing = [
        (e.event_type, tag_of_start(e) or tag_of_end(e))
        for e in [*starts, *ends]
        if call_id_of(e) is None
    ]
    assert not missing, f"legs with no call_id: {missing}"


async def test_two_traces_of_one_tool_get_distinct_ids() -> None:
    """A mint hoisted onto the CLIENT rather than the Trace sends one id for
    every call of that client — the library path's version of the hoisted mint,
    and the one this shape invites, since the client is the long-lived object
    and the Trace is the per-call one."""
    _, starts, _ = await _drive()
    start_ids = [call_id_of(e) for e in starts]
    assert all(i is not None for i in start_ids), f"a start had no call_id: {start_ids}"
    assert len(set(start_ids)) == 2, (
        f"two traces must mint two ids, got {start_ids} — a shared id pairs "
        "across calls of one tool, which is strictly worse than FIFO"
    )


async def test_call_id_pairs_each_leg_with_its_own_trace() -> None:
    gates, starts, ends = await _drive()
    assert_the_rig_inverted(gates, starts, ends)

    by_id = {call_id_of(s): tag_of_start(s) for s in starts}
    assert None not in by_id, f"a start had no call_id: {list(by_id)}"

    unjoined = [tag_of_end(e) for e in ends if call_id_of(e) not in by_id]
    assert not unjoined, (
        f"end legs whose call_id matches no start: {unjoined} — the id did not "
        "survive the whole call, so this leg can pair with nothing"
    )

    joined = sorted((by_id[call_id_of(e)], tag_of_end(e)) for e in ends)
    assert joined == [(FAST, FAST), (SLOW, SLOW)], (
        f"each end must join its OWN start, got {joined} — FIFO produces "
        f"{sorted(fifo_pairs(starts, ends))} on this same stream"
    )
