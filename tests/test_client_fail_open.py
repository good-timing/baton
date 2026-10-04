"""The LIBRARY path must not break the vendor's own code (SPEC §11.2 item 1).

`tests/integrations/standalone/test_fail_open_boundary.py` covers the MCP
middleware. This file covers `Client` / `AsyncClient`, where there is no tool
call we wrap: the vendor writes `with client.trace(...)` in their own source,
so a throw out of `__exit__` lands in THEIR call stack.

⚠ **The exception's IDENTITY is the assertion, not "did it raise".** When the
vendor's own exception is propagating, `__exit__` runs; a throw there REPLACES
their exception with ours (theirs surviving only as `__context__`). A test
asserting "something raised" passes against that defect, which is the shape
that already fooled a test once on this thread.

⚠ **The scrubber is SELECTIVE.** One that throws on everything also throws
inside every build thunk, which drops the events by design, so the only
surviving assertion is "the call worked" and every site reads the same.

Driven first as `baton-internal/spikes/client_failopen_1003/`, and each
assertion here was proven to red by mutating the guard it covers back out.
"""

from __future__ import annotations

from typing import Any

import pytest

from baton.client import AsyncClient, Client

SENTINEL = "BATON_SCRUBBER_BOOM"
VENDOR_MSG = "vendor-real-failure-row-42"


class SelectiveThrower:
    """Identity, except on values whose repr contains ``trip_on``."""

    def __init__(self, trip_on: str) -> None:
        self.trip_on = trip_on
        self.tripped = 0

    def __call__(self, value: Any) -> Any:
        if self.trip_on in repr(value):
            self.tripped += 1
            raise RuntimeError(SENTINEL)
        return value


class CollectingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def write(self, event: Any) -> None:
        self.events.append(event)

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


def _types(sink: CollectingSink) -> list[str]:
    return [e.event_type for e in sink.events]


@pytest.fixture
def sink() -> CollectingSink:
    return CollectingSink()


def _sync(sink: CollectingSink, scrubber: Any) -> Client:
    # No DSN: a DSN supplies the sink, and the two together are a config
    # error. The explicit sink is what lets these tests read the wire.
    return Client(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=scrubber)


def _async(sink: CollectingSink, scrubber: Any) -> AsyncClient:
    return AsyncClient(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=scrubber)


# --- the controls, which must be able to fail -------------------------------


def test_an_identity_scrubber_files_both_legs(sink: CollectingSink) -> None:
    """Without this, a rig that never reached Baton would satisfy every
    assertion below vacuously."""
    with _sync(sink, lambda v: v).trace(tool_name="fetch_row", params={"id": "row-42"}) as t:
        t.observed(result={"row": "row-42"})
    assert _types(sink) == ["tool_call_start", "tool_call_end"]


def test_the_vendor_s_own_exception_reaches_them_normally(sink: CollectingSink) -> None:
    with pytest.raises(ValueError, match=VENDOR_MSG):
        with _sync(sink, lambda v: v).trace(tool_name="fetch_row"):
            raise ValueError(VENDOR_MSG)
    assert _types(sink) == ["tool_call_start", "tool_call_error"]


# --- the two failure shapes -------------------------------------------------


def test_a_throwing_scrubber_does_not_escape_the_with_block(sink: CollectingSink) -> None:
    """Shape 1: the vendor's block completes. Reds without the construction
    guard in ``Client._emit_sync``."""
    thrower = SelectiveThrower("row-42")
    with _sync(sink, thrower).trace(tool_name="fetch_row", params={"id": "row-42"}) as t:
        t.observed(result={"row": "row-42"})
    assert thrower.tripped, "the scrubber was never reached; this proves nothing"


def test_the_END_event_is_DROPPED_rather_than_sent_with_a_null_result(
    sink: CollectingSink,
) -> None:
    """⚠ The one scrubber application on this surface that must NOT degrade.

    ``result=None`` on ``tool_call_end`` ALREADY means "``observed()`` was
    never called" (`_end_result_fields`), so answering ``None`` on a failed
    scrub publishes a fabricated "the tool returned nothing". Dropping the
    event is the only outcome that asserts nothing.
    """
    with _sync(sink, SelectiveThrower("row-42")).trace(tool_name="fetch_row") as t:
        t.observed(result={"row": "row-42"})
    assert _types(sink) == ["tool_call_start"]


def test_a_throwing_scrubber_does_not_REPLACE_the_vendor_s_exception(
    sink: CollectingSink,
) -> None:
    """Shape 2, and the one an "it did not raise" test cannot see."""
    thrower = SelectiveThrower(VENDOR_MSG)
    with pytest.raises(ValueError) as caught:
        with _sync(sink, thrower).trace(tool_name="fetch_row"):
            raise ValueError(VENDOR_MSG)
    assert str(caught.value) == VENDOR_MSG, "Baton's exception replaced the vendor's"
    assert thrower.tripped, "the scrubber was never reached; this proves nothing"


# --- the async twin, which is a copy and drifts like one --------------------


async def test_async_holds_both_shapes_too(sink: CollectingSink) -> None:
    thrower = SelectiveThrower("row-42")
    async with _async(sink, thrower).trace(tool_name="fetch_row") as t:
        t.observed(result={"row": "row-42"})
    assert _types(sink) == ["tool_call_start"]
    assert thrower.tripped

    sink2 = CollectingSink()
    vendor = SelectiveThrower(VENDOR_MSG)
    with pytest.raises(ValueError) as caught:
        async with _async(sink2, vendor).trace(tool_name="fetch_row"):
            raise ValueError(VENDOR_MSG)
    assert str(caught.value) == VENDOR_MSG
    assert vendor.tripped


# --- two regressions the builder refactor introduced ------------------------


def test_the_recorded_result_is_a_SNAPSHOT_not_the_vendor_s_live_object(
    sink: CollectingSink,
) -> None:
    """Mutating the result after ``observed()`` must not change what shipped.

    The refactor briefly stored the vendor's object and scrubbed at
    ``__exit__``, which recorded the value as it looked LATER — a pooled or
    reused buffer, or a handler that appends to its result after reporting
    it, would have the wrong contents recorded. On ``AsyncTrace`` the window
    spans every ``await`` in between.

    Snapshot semantics come from the default ``Scrubber`` rebuilding dicts
    and lists, so this asserts through the real scrubber rather than an
    identity one — an identity scrubber aliases and cannot see the bug.
    """
    rows: dict[str, Any] = {"rows": ["alice"]}
    with Client(tenant_id="ten_abc", vendor_id="v1", sink=sink).trace(tool_name="fetch_rows") as t:
        t.observed(result=rows)
        rows["rows"].append("LATE-MUTATION")
        rows["added_after"] = "observed()"

    end = next(e for e in sink.events if e.event_type == "tool_call_end")
    assert end.payload.result == {"rows": ["alice"]}, (
        f"the vendor's later mutation reached the wire: {end.payload.result}"
    )


def test_a_DISABLED_client_runs_no_scrubber_and_builds_no_event(
    monkeypatch: pytest.MonkeyPatch, sink: CollectingSink
) -> None:
    """The kill switch must stop the WORK, not just the write.

    An operator flips ``BATON_DISABLED`` precisely when our capture is
    implicated in an incident — often the scrubber itself. Building before
    the check ran the vendor's scrubber over every result and validated a
    pydantic model anyway. The async door already returned before its thunk;
    this pins the sync door to the same behaviour.
    """
    monkeypatch.setenv("BATON_DISABLED", "1")
    seen: list[Any] = []

    def counting(value: Any) -> Any:
        seen.append(value)
        return value

    with Client(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=counting).trace(
        tool_name="fetch_rows", params={"id": "row-42"}
    ) as t:
        t.observed(result={"row": "row-42"})

    assert sink.events == [], "a disabled client reached the sink"
    assert seen == [{"id": "row-42"}], (
        f"a disabled client still ran the scrubber over the RESULT: {seen}"
    )
