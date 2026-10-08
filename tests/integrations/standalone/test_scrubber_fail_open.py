"""A raising vendor scrubber MUST NOT break the vendor's tool call.

SPEC §11.2 / `fail_open_capture_boundary.md`. `safe_write` guards `sink.write`;
`safe_emit` guards payload CONSTRUCTION; `scrub_or_none` guards the scrubber
applications that run as plain statements outside any build thunk. This file
pins all three at the surface where the defect was measured.

⚠ **The thrower is SELECTIVE, and that is the point.** A scrubber that throws
on everything also throws inside the build thunks, which `safe_emit` drops by
design — so the only surviving assertion would be "the call worked" and every
site becomes indistinguishable. Throwing on ONE value keeps the rest of the
pipeline intact and names which site fired. Learned in the TS port, recorded
there in `safeScrub.ts`.

⚠ **Each test has a CONTROL that must pass**, because "the tool call
succeeded" is also what a fixture that never ran the tool looks like
(`feedback_control_condition_must_be_able_to_fail`).

Proven to redden against the pre-fix tree:
`baton-internal/spikes/python_scrubber_failopen_1002/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._failopen_helpers import (
    FLAG_IS_EXPRESSIBLE,
    SelectiveThrower,
)

pytestmark = pytest.mark.asyncio

VENDOR_MSG = "vendor-real-failure-row-42"
CLIENT_NAME = "mcp"  # what `fastmcp.Client` reports as its own name


class _Thrower:
    """Identity, except on values matching `trip`, where it raises."""

    def __init__(self, trip: str, *, contains: bool = False) -> None:
        self._trip, self._contains = trip, contains
        self.tripped = 0

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str) and (
            self._trip in value if self._contains else value == self._trip
        ):
            self.tripped += 1
            raise RuntimeError("scrubber exploded")
        return value


LEG_RETURNS = "returns"
LEG_RAISES = "raises"
LEG_ERROR_FLAG = "error_flag"


async def _drive(
    events_path: Path,
    scrubber: Any,
    *,
    leg: str = LEG_RETURNS,
    call: str | None = None,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Install Baton on a one-tool server, call something, report what came back.

    One rig for every probe in this file. There were four near-copies of it,
    differing only in the tool body and the tool called — so each new
    `VendorConfig` field had to be added four times, and `workflow` showed what
    a change made in one copy and not another costs.

    `leg` picks the `fetch` tool's behaviour; `call`/`args` default to calling
    `fetch` itself, and name the annotation tool instead when given. Returns
    `ok` / `error` / `text` / `events`.
    """
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.sinks import FileSink
    from tests._event_helpers import read_events
    from tests._failopen_helpers import error_result

    mcp: Any = FastMCP("failopen")

    @mcp.tool
    def fetch(row: str) -> Any:
        if leg == LEG_RAISES:
            raise ValueError(VENDOR_MSG)
        if leg == LEG_ERROR_FLAG:
            return error_result(RETURN_REASON)
        return {"record": f"row-{row}-ok"}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="failopen",
            vendor_display_name="Fail Open",
            consent_token="ct_failopen",
            sink=FileSink(str(events_path)),
            tenant_id="t-failopen",
            scrubber=scrubber,
        ),
    )
    out: dict[str, Any] = {"ok": False, "error": None, "text": "", "events": []}
    try:
        async with Client(mcp) as client:
            name = handle.annotation_tool_name if call == "annotate" else "fetch"
            try:
                res = await client.call_tool(name, args if args is not None else {"row": "42"})
                out["ok"] = True
                out["text"] = str(getattr(res, "content", res))
            except Exception as exc:
                out["error"] = str(exc)
    finally:
        await handle.aclose()
    if events_path.exists():
        out["events"] = read_events(events_path)
    return out


async def _call(events_path: Path, scrubber: Any, *, fail: bool) -> dict[str, Any]:
    """The original two-leg signature, kept so this file's first four probes
    read as they did. `fail=True` is the RAISE leg."""
    return await _drive(events_path, scrubber, leg=LEG_RAISES if fail else LEG_RETURNS)


async def test_control_a_working_tool_call_succeeds(tmp_path: Path) -> None:
    """Without this, every assertion below passes vacuously."""
    res = await _call(tmp_path / "c1.jsonl", lambda v: v, fail=False)
    assert res["ok"], res["error"]


async def test_control_a_failing_tool_surfaces_the_vendors_own_message(
    tmp_path: Path,
) -> None:
    res = await _call(tmp_path / "c2.jsonl", lambda v: v, fail=True)
    assert VENDOR_MSG in (res["error"] or "")


async def test_a_throwing_scrubber_does_not_break_a_working_call(tmp_path: Path) -> None:
    """`scrub_or_none` at `client_observed._clean` — fires on EVERY call."""
    thrower = _Thrower(CLIENT_NAME)
    res = await _call(tmp_path / "p1.jsonl", thrower, fail=False)
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert res["ok"], f"fail-open broken: {res['error']}"


async def test_a_throwing_scrubber_does_not_replace_the_vendors_error(
    tmp_path: Path,
) -> None:
    """`safe_emit` on the RAISE shape — the scrubber runs inside the thunk.

    ⚠ **This docstring named the wrong site until 2026-10-03.** It said
    ~~"`_error_result.py`, NOT the middleware's `except`: FastMCP converts a
    raising tool to a returned-flag result before the middleware sees it"~~ —
    measured on `fastmcp` 4.0.3, a raising tool propagates through `call_next`
    as `ToolError` and this probe trips at `middleware.py`'s RAISE leg, inside
    that `except`. The conversion claim is true of the `_error_result.py` site
    the 10-02 spike measured and false as a statement about the seam, which is
    why `returned_error_fields` went untested until a tool that RETURNS the
    flag was driven (below).
    """
    thrower = _Thrower(VENDOR_MSG, contains=True)
    res = await _call(tmp_path / "p2.jsonl", thrower, fail=True)
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert VENDOR_MSG in (res["error"] or ""), (
        f"our throw replaced the vendor's error: {res['error']}"
    )


# ---------------------------------------------------------------------------
# The sites `safe_emit` did NOT reach until 2026-10-03: a projection or an
# event construction evaluated as a STATEMENT, or as an argument, in the
# caller's frame. `safe_emit` guards only what the thunk it is handed
# evaluates, so each of these escaped it entirely
# (`feedback_a_guard_does_not_cover_its_callers_arguments`).
#
# ⚠ Each asserts `tripped` first. Without it a rig that never reached the
# site reads exactly like a working guard.
# ---------------------------------------------------------------------------

RESULT_MARKER = "row-42-ok"  # appears ONLY in the tool's return value
RETURN_REASON = "you do not have sufficient access"
GOAL_MARKER = "ship-the-thing"

# ⚠ `fastmcp` 2.14.7 is the `fastmcp-matrix` FLOOR (`ci.yml`) and its
# `ToolResult.__init__` takes no `is_error` at all, so a returned-error-flag
# fixture raises `TypeError` there rather than failing an assertion. Measured:
# without this guard that leg is `2 failed, 7 passed`. The predicate lives in
# `tests/_failopen_helpers.py` because a second copy drifts through exactly the
# floor bump `test_iserror_reclassify.py::test_floor_has_no_flag_to_read`
# exists to catch.
_needs_flag = pytest.mark.skipif(
    not FLAG_IS_EXPRESSIBLE,
    reason="fastmcp floor (2.14.7) cannot express a returned error flag; "
    "test_iserror_reclassify.py::test_floor_has_no_flag_to_read pins that absence",
)


async def test_a_throwing_scrubber_on_the_END_leg_does_not_break_the_call(
    tmp_path: Path,
) -> None:
    """`end_result_fields` — a statement before `safe_emit` until 10-03."""
    thrower = SelectiveThrower(RESULT_MARKER)
    res = await _call(tmp_path / "p3.jsonl", thrower, fail=False)
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert res["ok"], f"fail-open broken on the END leg: {res['error']}"


@_needs_flag
async def test_control_a_returned_error_flag_reaches_the_client(tmp_path: Path) -> None:
    """Without this the RETURN-leg test below cannot distinguish a guard from
    a rig whose tool never returned the flag."""
    res = await _drive(tmp_path / "c3.jsonl", lambda v: v, leg=LEG_ERROR_FLAG)
    assert res["ok"] or RETURN_REASON in (res["error"] or ""), res
    # ⚠ The line above cannot fail on its own: `ok` is True for any `call_tool`
    # that does not raise, including a rig whose tool never set the flag —
    # which is the rig this control exists to catch
    # (`feedback_control_condition_must_be_able_to_fail`). The emitted
    # `tool_call_error` is what proves the producer read a flag.
    kinds = [e["event_type"] for e in res["events"]]
    assert "tool_call_error" in kinds, (
        f"the tool's error flag never reached the producer; the RETURN probe "
        f"below would be testing the END leg: {kinds}"
    )


@_needs_flag
async def test_a_throwing_scrubber_on_the_RETURN_leg_does_not_break_the_call(
    tmp_path: Path,
) -> None:
    """`returned_error_fields` — a statement before `safe_emit` until 10-03."""
    thrower = SelectiveThrower(RETURN_REASON)
    res = await _drive(tmp_path / "p4.jsonl", thrower, leg=LEG_ERROR_FLAG)
    # ⚠ `tripped` alone does NOT name the projection: the END projection also
    # scrubs a value whose repr carries the reason, so this test passes
    # unchanged if the error flag went unread and only `end_result_fields`
    # ran — proven by forcing `is_error_result` to False. The RETURN
    # projection scrubs `error_text(result)`, a bare string; the END one
    # scrubs the serialised envelope.
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert thrower.tripped_on == RETURN_REASON, (
        "the END projection fired, not the RETURN one; this test is a duplicate "
        f"of the END-leg test as written: {thrower.tripped_on!r}"
    )
    assert res["ok"] or RETURN_REASON in (res["error"] or ""), (
        f"fail-open broken on the RETURN leg: {res['error']}"
    )


async def test_control_the_annotation_tool_accepts_a_goal(tmp_path: Path) -> None:
    res = await _drive(
        tmp_path / "c4.jsonl",
        lambda v: v,
        call="annotate",
        args={"user_goal": GOAL_MARKER, "signal_type": "failure"},
    )
    assert res["ok"], res["error"]


async def test_a_throwing_scrubber_does_not_break_the_ANNOTATION_tool(
    tmp_path: Path,
) -> None:
    """`_annotate`'s own `AnnotationEvent(...)` — built in the caller's frame
    and handed to `safe_write` until 10-03.

    ⚠ `_annotate` is a tool on the VENDOR's server, so a throw here surfaces
    to their end user as their server erroring — the ownership defence ("it
    only breaks Baton's own call") is true of ownership and false of what the
    user sees.
    """
    thrower = _Thrower(GOAL_MARKER)
    res = await _drive(
        tmp_path / "p5.jsonl",
        thrower,
        call="annotate",
        args={"user_goal": GOAL_MARKER, "signal_type": "failure"},
    )
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert res["ok"], f"fail-open broken on the annotation path: {res['error']}"


async def test_the_annotation_workflow_field_goes_through_the_scrubber(
    tmp_path: Path,
) -> None:
    """SPEC §11.2(2) — annotation text is scrubbed, and `workflow` is text.

    ⚠ It was NOT, until 2026-10-03: the rename that produced this wire key
    carried the expression across unscrubbed. Two consequences, and the second
    is why this is more than a leak: `middleware.py` scrubs the same semantic
    field onto `call_workflow`, so a rewriting scrubber emitted two different
    values for the one key the Console groups on.
    """
    res = await _drive(
        tmp_path / "w1.jsonl",
        lambda v: v.replace("alice@acme.com", "[REDACTED]") if isinstance(v, str) else v,
        call="annotate",
        args={
            "user_goal": "goal alice@acme.com",
            "overall_task": "task alice@acme.com",
            "signal_type": "failure",
        },
    )
    events = res["events"]
    ann = [e for e in events if e.get("event_type") == "annotation"]
    assert ann, f"no annotation event written: {events!r}"
    payload = ann[0]["payload"]
    # The control: the field that was ALREADY scrubbed. Without it a scrubber
    # the rig never wired up reads exactly like a working one.
    assert payload["intent"] == "goal [REDACTED]", payload
    assert payload["workflow"] == "task [REDACTED]", (
        f"`workflow` bypassed the vendor's scrubber: {payload['workflow']!r}"
    )
