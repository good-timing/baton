"""The fail-open matrix, standalone-adapter row: a scrubber that throws on EVERYTHING.

Four legs (success, raise, error-result, annotate). Each cell drives the same
call twice, once with an identity scrubber and once with ``ThrowAll``, and
asserts the vendor's caller sees the same thing both times. The library half
and the reason for a throw-all scrubber are in
``tests/test_client_fail_open_matrix.py``.

The rig is ``test_scrubber_fail_open.py``'s.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._failopen_helpers import FLAG_IS_EXPRESSIBLE, SENTINEL, ThrowAll
from tests.integrations.standalone.test_scrubber_fail_open import (
    GOAL_MARKER,
    LEG_ERROR_FLAG,
    LEG_RAISES,
    LEG_RETURNS,
    RESULT_MARKER,
    RETURN_REASON,
    VENDOR_MSG,
    _drive,
)

pytestmark = pytest.mark.asyncio

_ANNOTATE_ARGS = {"user_goal": GOAL_MARKER, "overall_task": "a task", "signal_type": "failure"}


def _seen(res: dict[str, Any]) -> tuple[bool, str | None, str]:
    return res["ok"], res["error"], res["text"]


async def _both(tmp_path: Path, **drive: Any) -> tuple[Any, Any, ThrowAll]:
    thrower = ThrowAll()
    baseline = await _drive(tmp_path / "identity.jsonl", lambda v: v, **drive)
    thrown = await _drive(tmp_path / "throwall.jsonl", thrower, **drive)
    return baseline, thrown, thrower


async def test_success(tmp_path: Path) -> None:
    baseline, thrown, thrower = await _both(tmp_path, leg=LEG_RETURNS)
    assert RESULT_MARKER in baseline["text"], f"the rig is broken: {baseline}"
    assert thrower.saw(RESULT_MARKER), (
        f"this leg's value never reached the scrubber: {thrower.seen!r}"
    )
    assert _seen(thrown) == _seen(baseline)


async def test_raise(tmp_path: Path) -> None:
    baseline, thrown, thrower = await _both(tmp_path, leg=LEG_RAISES)
    assert VENDOR_MSG in (baseline["error"] or ""), f"the rig is broken: {baseline}"
    assert thrower.saw(VENDOR_MSG), f"this leg's value never reached the scrubber: {thrower.seen!r}"
    assert _seen(thrown) == _seen(baseline)
    assert SENTINEL not in (thrown["error"] or "")


@pytest.mark.skipif(
    not FLAG_IS_EXPRESSIBLE,
    reason="this fastmcp's ToolResult has no is_error, so a tool cannot return the flag",
)
async def test_error_result(tmp_path: Path) -> None:
    baseline, thrown, thrower = await _both(tmp_path, leg=LEG_ERROR_FLAG)
    assert RETURN_REASON in baseline["text"] + (baseline["error"] or ""), baseline
    assert "tool_call_error" in [e["event_type"] for e in baseline["events"]], (
        "the error flag never reached the producer; this cell would test the success leg"
    )
    # The bare string, not a value containing it: the success projection
    # scrubs an envelope that carries the same text.
    assert RETURN_REASON in thrower.seen, (
        f"the returned-error text was never scrubbed: {thrower.seen!r}"
    )
    assert _seen(thrown) == _seen(baseline)


async def test_annotate(tmp_path: Path) -> None:
    baseline, thrown, thrower = await _both(tmp_path, call="annotate", args=_ANNOTATE_ARGS)
    assert baseline["ok"] and "ok" in baseline["text"].lower(), f"the rig is broken: {baseline}"
    assert thrower.saw(GOAL_MARKER), (
        f"this leg's value never reached the scrubber: {thrower.seen!r}"
    )
    assert _seen(thrown) == _seen(baseline)
