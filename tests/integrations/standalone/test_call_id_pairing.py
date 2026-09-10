"""``call_id`` must pair a call's legs when completion order INVERTS —
standalone ``fastmcp`` adapter.

The sibling of ``tests/integrations/official/test_call_id_pairing.py``, and it
exists as a separate file for the reason ``test_agent_runtime_parity.py``
records: a capability that lives in one adapter's package and is never called
by the other survived a rename, a release and the CI matrix, because each
adapter's suite asserted only about itself. The mint lands on both emit paths
or it has not landed. **``fastmcp-matrix`` runs this directory** against fastmcp
2.14.7 / 3.4.2 / 4.0.2, which is the only place the floor's mcp 1.30 and
fastmcp 4's mcp 2.2 are exercised.

Both files are strict ``xfail``s written before the mint (workplan N2a); see
the official file's docstring for why that order matters, and
``tests/_forced_reorder.py`` for the rig.

One measured detail that shaped the rig and belongs here: on **fastmcp 2.14.7**
the ``tool_call_end`` payload carries ``result`` as an opaque
``<fastmcp.tools.tool.ToolResult object at 0x…>`` repr and ``duration_ms: 0``,
so on that leg neither the result body nor the duration can say which call an
end belongs to. The ground truth is the event TYPE instead — the FAST call
raises — which reads identically on every version of both libraries.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from baton.events import Event
from baton.integrations.standalone import VendorConfig, install_baton
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
    mcp: FastMCP[Any] = FastMCP("call-id-standalone")

    @mcp.tool
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
        async with Client(mcp) as client:
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

    Passes today and must keep passing after the mint — it asserts about the
    emitted STREAM, which the mint does not change. Without it, a rig that
    quietly stopped overlapping would let the pairing tests pass for the wrong
    reason, which is the failure V2 shipped.
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
    """Separate from the join assertion on purpose — see the official file: a
    mint hoisted out of per-call scope pairs ACROSS calls of one tool, which is
    strictly worse than the FIFO floor it outranks, and only distinctness
    catches it."""
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

    joined = sorted(
        (by_id.get(call_id_of(e)), tag_of_end(e))
        for e in ends  # type: ignore[arg-type]
    )
    assert joined == [(FAST, FAST), (SLOW, SLOW)], (
        f"each end must join its OWN start, got {joined} — FIFO produces "
        f"{sorted(fifo_pairs(starts, ends))} on this same stream"
    )
