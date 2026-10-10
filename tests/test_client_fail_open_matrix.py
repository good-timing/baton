"""The fail-open matrix, library half: a scrubber that throws on EVERYTHING.

One row of cells per surface (sync ``Client``, async ``AsyncClient``), one
column per leg (success, raise, error-result, annotate). Each cell asserts the
vendor's own code runs to the end and, on the raise leg, that the exception
they catch is the very object they raised. The adapter halves are
``tests/integrations/{official,standalone}/test_fail_open_matrix.py``.

``test_client_fail_open.py`` uses a selective thrower because it asserts HOW
each site degrades. This file asserts only that nothing reaches the vendor, so
every site reading the same is the property.
"""

from __future__ import annotations

from typing import Any

import pytest

from baton.client import AsyncClient, Client
from baton.events import Event
from baton.sinks import Sink
from tests._failopen_helpers import SENTINEL, ThrowAll

# Each leg's own value, which must reach the scrubber for the cell to count.
LEG_MARKER = {
    "success": "result-row-42",
    "raise": "vendor-real-failure-row-42",
    "error_result": "returned-failure-row-42",
    "annotate": "annotate-intent-42",
}
LEGS = tuple(LEG_MARKER)
IN_TRACE_ANNOTATION = "in-trace-annotation-42"


class _NullSink(Sink):
    async def write(self, event: Event) -> None: ...

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


class _VendorError(Exception):
    pass


def _drive_sync(scrubber: Any, leg: str) -> BaseException | None:
    """Run one leg of vendor code; return what the vendor's ``except`` caught."""
    client = Client(tenant_id="ten_abc", vendor_id="v1", sink=_NullSink(), scrubber=scrubber)
    raised = _VendorError(LEG_MARKER["raise"])
    try:
        if leg == "annotate":
            client.annotate(
                what_happened="it failed", intent=LEG_MARKER["annotate"], context={"row": "42"}
            )
            return None
        try:
            with client.trace(tool_name="fetch_row", params={"id": "row-42"}) as t:
                if leg == "success":
                    t.observed(result={"row": LEG_MARKER["success"]})
                elif leg == "error_result":
                    t.observed(error=ValueError(LEG_MARKER["error_result"]))
                else:
                    raise raised
                t.annotate(what_happened=IN_TRACE_ANNOTATION)
        except _VendorError as caught:
            assert caught is raised, "the vendor caught a different exception object"
            return caught
        return None
    finally:
        client.close()


async def _drive_async(scrubber: Any, leg: str) -> BaseException | None:
    client = AsyncClient(tenant_id="ten_abc", vendor_id="v1", sink=_NullSink(), scrubber=scrubber)
    raised = _VendorError(LEG_MARKER["raise"])
    try:
        if leg == "annotate":
            await client.annotate(
                what_happened="it failed", intent=LEG_MARKER["annotate"], context={"row": "42"}
            )
            return None
        try:
            async with client.trace(tool_name="fetch_row", params={"id": "row-42"}) as t:
                if leg == "success":
                    t.observed(result={"row": LEG_MARKER["success"]})
                elif leg == "error_result":
                    t.observed(error=ValueError(LEG_MARKER["error_result"]))
                else:
                    raise raised
                await t.annotate(what_happened=IN_TRACE_ANNOTATION)
        except _VendorError as caught:
            assert caught is raised, "the vendor caught a different exception object"
            return caught
        return None
    finally:
        await client.aclose()


def _assert_cell(leg: str, thrower: ThrowAll, caught: BaseException | None) -> None:
    assert thrower.saw(LEG_MARKER[leg]), (
        f"this leg's value never reached the scrubber; the cell proves nothing: {thrower.seen!r}"
    )
    if leg in ("success", "error_result"):
        assert thrower.saw(IN_TRACE_ANNOTATION), thrower.seen
    if leg == "raise":
        assert isinstance(caught, _VendorError), f"the vendor's exception was lost: {caught!r}"
        assert SENTINEL not in str(caught)
    else:
        assert caught is None


@pytest.mark.parametrize("leg", LEGS)
def test_sync_client(leg: str) -> None:
    thrower = ThrowAll()
    _assert_cell(leg, thrower, _drive_sync(thrower, leg))


@pytest.mark.parametrize("leg", LEGS)
async def test_async_client(leg: str) -> None:
    thrower = ThrowAll()
    _assert_cell(leg, thrower, await _drive_async(thrower, leg))
