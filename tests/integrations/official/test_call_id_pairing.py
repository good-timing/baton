"""``call_id`` must pair a call's legs when completion order INVERTS — official
mcp SDK adapter.

**Written before the field exists (workplan N2a).** The pairing tests are strict
``xfail``s: they red today on their own assertions, XPASS the day the mint lands
(N2-SDK), and a strict marker turns that XPASS into a suite failure, so the
marker cannot outlive the fix. That order is deliberate — a correlation test
written after its fix tends to be written around the implementation it is
supposed to police.

Here rather than in ``tests/functional/`` for the reason ``test_user_id.py`` and
``test_agent_runtime.py`` are: **``mcp-matrix`` runs
``tests/integrations/official/`` and nothing else**, against mcp 1.20.0 /
1.25.0 / 1.27.2 / 2.0.0. Whether one session even carries two concurrent
``tools/call`` requests is a per-version property of the server library, so a
home outside this directory would test it on exactly one resolve — the same gap
that let V4 answer which field to read without ever running the floors.
Measured 2026-09-09 before this file was written: all four legs process the two
calls concurrently and produce the inversion.

The rig, the ground truth, and why the ground truth is the event TYPE rather
than the result body, all live in ``tests/_forced_reorder.py``.

The third emit surface — the library API's ``Trace`` / ``AsyncTrace`` — is
pinned by ``tests/test_call_id_pairing_library.py``. All three or the mint
has not landed.

The tier these tests are about is SPEC §11.5.4. ``baton-console`` already
implements it and keys on ``(call_id, tool_name)``; the reason for that compound
key is why distinctness is asserted separately from the join.
"""

from __future__ import annotations

import asyncio

import pytest

from baton.events import Event
from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from tests._forced_reorder import (
    FAST,
    SLOW,
    CapturingSink,
    ReorderGates,
    assert_the_rig_inverted,
    call_id_of,
    fifo_pairs,
    legs,
    tag_of_end,
    tag_of_start,
)
from tests._mcp_session import connected_session

_MINT_PENDING = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "no adapter mints a call_id yet — workplan N2-SDK. The field is "
        "specified (SPEC §11.4) and baton-console already pairs on it "
        "(§11.5.4); this is the SDK half. Remove this marker with the mint."
    ),
)


async def _drive() -> tuple[ReorderGates, list[Event], list[Event]]:
    """Two overlapping calls to one tool on one session, the slow one first."""
    gates = ReorderGates()
    sink = CapturingSink()
    mcp = FastMCP("call-id-official")

    @mcp.tool()
    async def work(tag: str) -> dict[str, str]:
        return await gates.tool_body(tag)

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="pairing",
            vendor_display_name="Pairing Vendor",
            consent_token="ct_pairing",
            sink=sink,
            tenant_id="tenant-pairing",
        ),
    )
    try:
        async with connected_session(mcp) as client:
            slow = asyncio.create_task(client.call_tool("work", {"tag": SLOW}))
            # Only launch FAST once SLOW is demonstrably inside the handler:
            # the start ORDER is half the rig, and gather() alone does not fix
            # which request the server picks up first.
            await gates.wait_until_slow_is_inside()
            fast = asyncio.create_task(client.call_tool("work", {"tag": FAST}))
            await asyncio.gather(slow, fast, return_exceptions=True)
    finally:
        await handle.aclose()

    starts, ends = legs(sink.events)
    return gates, starts, ends


async def test_the_rig_inverts_and_fifo_mispairs_because_of_it() -> None:
    """The green half: prove the run exercised the defect.

    This test passes today and must keep passing after the mint — it asserts
    about the emitted STREAM, which the mint does not change. Its job is to
    stop the ``xfail``s below from being satisfied by a run in which nothing
    overlapped: a rig that quietly stopped inverting would make a pairing test
    pass for the wrong reason, which is the exact failure V2 shipped.
    """
    gates, starts, ends = await _drive()
    assert_the_rig_inverted(gates, starts, ends)

    pairs = fifo_pairs(starts, ends)
    assert pairs == [(SLOW, FAST), (FAST, SLOW)], (
        f"FIFO should mispair both legs on this stream, got {pairs}"
    )
    assert all(start != end for start, end in pairs), (
        "every FIFO pair here joins one call's arguments to the other call's "
        "outcome — the call that SUCCEEDED is recorded as having raised. That "
        "is what the call_id tier exists to fix"
    )


@_MINT_PENDING
async def test_every_leg_of_a_call_carries_a_call_id() -> None:
    _, starts, ends = await _drive()
    missing = [
        (e.event_type, tag_of_start(e) or tag_of_end(e))
        for e in [*starts, *ends]
        if call_id_of(e) is None
    ]
    assert not missing, f"legs with no call_id: {missing}"


@_MINT_PENDING
async def test_the_two_calls_get_distinct_ids() -> None:
    """Separate from the join assertion on purpose.

    A mint hoisted out of per-call scope — onto a module-level or per-session
    variable — sends one constant id for every call in the session. Both calls
    here hit the SAME tool, so ``(call_id, tool_name)`` collapses to one queue
    and the tier degrades to the FIFO floor it outranks while still looking
    like the top tier on the wire. Distinctness is the property that catches
    it, and it is invisible to a join test that pairs two calls to two
    different tools.
    """
    _, starts, _ = await _drive()
    start_ids = [call_id_of(e) for e in starts]
    assert all(i is not None for i in start_ids), f"a start had no call_id: {start_ids}"
    assert len(set(start_ids)) == 2, (
        f"two calls must mint two ids, got {start_ids} — a shared id pairs "
        "across calls of one tool, which is strictly worse than FIFO"
    )


@_MINT_PENDING
async def test_call_id_pairs_each_leg_with_its_own_start() -> None:
    """The assertion the whole file exists for: the join is right where FIFO's
    is wrong."""
    gates, starts, ends = await _drive()
    assert_the_rig_inverted(gates, starts, ends)

    by_id = {call_id_of(s): tag_of_start(s) for s in starts}
    assert None not in by_id, f"a start had no call_id: {list(by_id)}"

    # Before joining: every end must resolve to a start. A mint threaded
    # through the success path but NOT the error path — the branch the failing
    # FAST leg exists to expose — leaves one end unresolvable, and sorting
    # ``None`` beside a string raises TypeError, which a strict
    # ``xfail(raises=AssertionError)`` reports as an opaque error rather than
    # the diagnostic below.
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
