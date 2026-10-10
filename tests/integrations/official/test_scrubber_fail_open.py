"""A raising vendor scrubber MUST NOT break the vendor's tool call — official adapter.

The standalone twin is `tests/integrations/standalone/test_scrubber_fail_open.py`
and carries the full rationale. This file exists because the AST said the two
surfaces had the same shape, and on the standalone one the AST turned out to be
right about the defect and WRONG about which site fired — so this surface is
measured rather than inferred from that one.

⚠ The thrower is SELECTIVE and every probe has a CONTROL that must pass; see
the standalone twin for why both are load-bearing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._failopen_helpers import SENTINEL, SelectiveThrower

pytestmark = pytest.mark.asyncio

VENDOR_MSG = "vendor-real-failure-row-42"


class _Thrower:
    def __init__(self, trip: str, *, contains: bool = False) -> None:
        self._trip, self._contains = trip, contains
        self.tripped = 0
        self.seen: list[Any] = []

    def __call__(self, value: Any) -> Any:
        self.seen.append(value)
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

    One rig for every probe in this file; there were four near-copies, differing
    only in the tool body and the tool called. See the standalone twin.

    ⚠ On THIS adapter `ok` is not evidence of fail-open: `mcp`'s server catches
    an escaping throw and converts it to an error RESULT, so `call_tool` returns
    and `ok` is True while the vendor's answer has been replaced. Assert on
    `text`.
    """
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink
    from tests._event_helpers import read_events
    from tests._failopen_helpers import error_call_tool_result
    from tests._mcp_session import connected_session

    mcp = FastMCP("failopen-official")

    @mcp.tool()
    def fetch(row: str) -> Any:
        if leg == LEG_RAISES:
            raise ValueError(VENDOR_MSG)
        if leg == LEG_ERROR_FLAG:
            return error_call_tool_result(RETURN_REASON)
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
        async with connected_session(mcp) as client:
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
    """The original two-leg signature, kept so this file's first probes read as
    they did. `fail=True` is the RAISE leg."""
    return await _drive(events_path, scrubber, leg=LEG_RAISES if fail else LEG_RETURNS)


async def test_control_a_working_call_succeeds(tmp_path: Path) -> None:
    res = await _call(tmp_path / "c1.jsonl", lambda v: v, fail=False)
    assert res["ok"], res["error"]


async def test_control_what_the_scrubber_is_handed(tmp_path: Path) -> None:
    """Names the trip values the probes below use, instead of guessing them."""
    rec = _Thrower("\x00never")
    await _call(tmp_path / "c2.jsonl", rec, fail=False)
    strings = [v for v in rec.seen if isinstance(v, str)]
    assert strings, f"no string reached the scrubber; probes cannot target it: {rec.seen!r}"


async def test_a_throwing_scrubber_does_not_break_a_working_call(tmp_path: Path) -> None:
    rec = _Thrower("\x00never")
    await _call(tmp_path / "p0.jsonl", rec, fail=False)
    target = next(v for v in rec.seen if isinstance(v, str))

    thrower = _Thrower(target)
    res = await _call(tmp_path / "p1.jsonl", thrower, fail=False)
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    # ⚠ NOT `assert res["ok"]`. On this surface the server CATCHES our
    # exception and returns it as the tool's own result, so `call_tool` does
    # not raise and `ok` is True either way — the first version of this test
    # asserted exactly that and passed against the unfixed code. The agent
    # receives our internal error text as the tool's answer, so the result
    # CONTENT is the only thing that discriminates.
    assert "row-42-ok" in res["text"], (
        f"fail-open broken: the caller got our error instead of the tool's result: {res['text']}"
    )


# --- the two sites the tests above could not reach -------------------------
#
# Found 2026-10-03 by teaching `ast_sweep.py` the guard shapes this thread
# introduced. `emit_before` / `emit_error` build inside a `safe_emit` thunk,
# but the scrubber in their ARGUMENTS runs before that guard is entered, so
# `eb4fb8f` converted the construction and left these two live.


class _DictThrower:
    """Trips on a DICT, which is why the string thrower above missed a site.

    ``scrubber(params)`` on the start leg is handed the arguments dict.
    ``_Thrower`` guards every trip with ``isinstance(value, str)``, so it
    could never fire there however the target was chosen — the site was
    unreachable by the existing probes rather than guarded
    → [[feedback_a_negative_test_must_be_able_to_fail]].
    """

    def __init__(self) -> None:
        self.tripped = 0

    def __call__(self, value: Any) -> Any:
        if isinstance(value, dict):
            self.tripped += 1
            raise RuntimeError("scrubber exploded on params")
        return value


async def test_a_scrubber_throwing_on_PARAMS_does_not_break_the_call(tmp_path: Path) -> None:
    thrower = _DictThrower()
    res = await _call(tmp_path / "p2.jsonl", thrower, fail=False)
    assert thrower.tripped, "the probe never reached the params site; it proves nothing"
    # Same discriminator as the test above: this surface returns our exception
    # AS the tool's answer, so "did not raise" passes against the defect.
    assert "row-42-ok" in res["text"], (
        f"fail-open broken on the start leg: caller got our error: {res['text']}"
    )


async def _run_direct(events_path: Path, scrubber: Any) -> BaseException:
    """Drive the wrapped tool directly and hand back what it raised.

    ⚠ Not through a client session, and the reason is a CONTROL THAT FAILED.
    The first version of the test below asserted the vendor's message reaches
    the caller — it does not, with or without a scrubber: mcp 2.x masks every
    handler exception as ``UnexpectedToolError("Error executing tool fetch")``
    before the client sees it. So the caller-visible text is identical whether
    fail-open holds or not, and asserting on it proves nothing either way
    → [[feedback_control_condition_must_be_able_to_fail]]. What differs is the
    ``__cause__`` the SDK wraps, which is visible here and nowhere else.
    """
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink

    mcp = FastMCP("failopen-official-direct")

    @mcp.tool()
    def fetch(row: str) -> dict[str, Any]:
        raise ValueError(VENDOR_MSG)

    install_baton(
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
    tool = mcp._tool_manager.get_tool("fetch")
    try:
        await tool.run({"row": "42"}, None, convert_result=True)
    except BaseException as e:  # the exception itself is the test's subject
        return e
    raise AssertionError("the tool did not raise; the rig is broken, not the code")


async def test_a_throwing_scrubber_does_not_REPLACE_the_vendor_s_own_error(
    tmp_path: Path,
) -> None:
    """The RAISE leg, which no test here reached.

    ``emit_error``'s arguments are evaluated inside the vendor's ``except``,
    with a ``raise`` below them, so a throw there substituted OUR exception
    for theirs. The assertion is on the vendor's message surviving, not on
    something having gone wrong.
    """
    control = await _run_direct(tmp_path / "c3.jsonl", lambda v: v)
    assert isinstance(control.__cause__, ValueError) and VENDOR_MSG in str(control.__cause__), (
        f"CONTROL: the vendor's exception is not the cause even without a "
        f"throwing scrubber; the rig is broken, not the code: {control.__cause__!r}"
    )

    thrower = _Thrower(VENDOR_MSG, contains=True)
    raised = await _run_direct(tmp_path / "p3.jsonl", thrower)
    assert thrower.tripped, "the probe never reached the error leg; it proves nothing"
    assert isinstance(raised.__cause__, ValueError) and VENDOR_MSG in str(raised.__cause__), (
        f"fail-open broken on the raise leg: the vendor's ValueError was "
        f"REPLACED by ours — the cause is {raised.__cause__!r}"
    )


# ---------------------------------------------------------------------------
# The sites `safe_emit` did NOT reach until 2026-10-03 on this surface: the
# `end_result_fields` / `returned_error_fields` projections and `_annotate`'s
# own event, all evaluated in the CALLER's frame — a statement or an argument,
# either way outside the thunk `safe_emit` guards
# (`feedback_a_guard_does_not_cover_its_callers_arguments`).
#
# ⚠ The RAISE leg is NOT retested here: `cab0143` already guarded it with a
# caller-side try/except and `test_a_throwing_scrubber_does_not_REPLACE_the_
# vendor_s_own_error` above pins it. The builder change moved it inside the
# thunk; that test keeps it honest either way.
# ---------------------------------------------------------------------------

RESULT_MARKER = "row-42-ok"  # appears ONLY in the tool's return value
RETURN_REASON = "you do not have sufficient access"
GOAL_MARKER = "ship-the-thing"


async def test_a_throwing_scrubber_on_the_END_leg_does_not_break_the_call(
    tmp_path: Path,
) -> None:
    """`end_result_fields` — a statement before `safe_emit` until 10-03."""
    thrower = SelectiveThrower(RESULT_MARKER)
    res = await _call(tmp_path / "p3.jsonl", thrower, fail=False)
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    # ⚠ `res["ok"]` alone does NOT test this. On this surface an escaping throw
    # is caught by `mcp`'s server and converted into an error RESULT, so
    # `call_tool` returns and `ok` is True while the vendor's answer has been
    # replaced by "scrubber exploded". The surviving ANSWER is the property.
    assert RESULT_MARKER in (res["text"] or ""), (
        f"fail-open broken on the END leg — vendor's result replaced: {res['text']}"
    )


async def test_control_a_returned_error_flag_reaches_the_client(tmp_path: Path) -> None:
    """Without this the RETURN-leg probe cannot tell a guard from a rig whose
    tool never produced the flag.

    ⚠ Asserts the FLAG, not the reason. `RETURN_REASON` is the result's own
    content text, so it reaches the client whether or not `isError` is set — a
    control phrased on it passes for exactly the rig it exists to catch
    (`feedback_control_condition_must_be_able_to_fail`). The emitted
    `tool_call_error` is what proves the producer read a flag.
    """
    res = await _drive(tmp_path / "c3.jsonl", lambda v: v, leg=LEG_ERROR_FLAG)
    assert RETURN_REASON in (res["text"] or "") or RETURN_REASON in (res["error"] or ""), res
    kinds = [e["event_type"] for e in res["events"]]
    assert "tool_call_error" in kinds, (
        f"the tool's error flag never reached the producer; the RETURN probe "
        f"below would be testing the END leg: {kinds}"
    )


async def test_a_throwing_scrubber_on_the_RETURN_leg_does_not_break_the_call(
    tmp_path: Path,
) -> None:
    """`returned_error_fields` — an ARGUMENT to `emit_error` until 10-03."""
    thrower = SelectiveThrower(RETURN_REASON)
    res = await _drive(tmp_path / "p4.jsonl", thrower, leg=LEG_ERROR_FLAG)
    # ⚠ `tripped` alone does NOT name the projection — see `SelectiveThrower`.
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert thrower.tripped_on == RETURN_REASON, (
        "the END projection fired, not the RETURN one; this test is a duplicate "
        f"of the END-leg test as written: {thrower.tripped_on!r}"
    )
    assert RETURN_REASON in (res["text"] or "") or RETURN_REASON in (res["error"] or ""), (
        f"fail-open broken on the RETURN leg: {res['error']} / {res['text']}"
    )


async def test_control_the_annotation_tool_accepts_a_goal(tmp_path: Path) -> None:
    res = await _drive(
        tmp_path / "c4.jsonl",
        lambda v: v,
        call="annotate",
        args={"user_goal": GOAL_MARKER, "what_happened": "the call came back unusable"},
    )
    assert res["ok"], res["error"]
    assert "ok" in (res["text"] or "").lower(), (
        f"the probe below reads this text; name it here: {res['text']}"
    )


async def test_a_throwing_scrubber_does_not_break_the_ANNOTATION_tool(
    tmp_path: Path,
) -> None:
    """`_annotate`'s own `AnnotationEvent(...)`, built in the caller's frame and
    handed to `safe_write` until 10-03.

    ⚠ `_annotate` is a tool on the VENDOR's server, so a throw here surfaces to
    their end user as their server erroring.
    """
    # SelectiveThrower, not the file-local `_Thrower`: the assertion below
    # discriminates on OUR error text, and only this thrower raises `SENTINEL`.
    # With `_Thrower` ("scrubber exploded") that conjunct could never be False.
    thrower = SelectiveThrower(GOAL_MARKER)
    res = await _drive(
        tmp_path / "p5.jsonl",
        thrower,
        call="annotate",
        args={"user_goal": GOAL_MARKER, "what_happened": "the call came back unusable"},
    )
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    # Same trap as the END leg: an escaping throw comes back as an error RESULT
    # whose text is ours, so `ok` is True either way. `_annotate` answers
    # `{"ok": True}`; that is what must survive.
    assert "ok" in (res["text"] or "").lower() and SENTINEL not in (res["text"] or ""), (
        f"fail-open broken on the annotation path: {res['text']} / {res['error']}"
    )


async def test_the_annotation_workflow_field_goes_through_the_scrubber(
    tmp_path: Path,
) -> None:
    """SPEC §11.2(2) — the official twin of the standalone test.

    ⚠ Both adapters carried the same unscrubbed `workflow` and the same
    one-line fix, so testing one would leave a SPEC §11.2(2) fix half proven on
    a path the vendor believes is scrubbed.
    """
    res = await _drive(
        tmp_path / "w1.jsonl",
        lambda v: v.replace("alice@acme.com", "[REDACTED]") if isinstance(v, str) else v,
        call="annotate",
        args={
            "user_goal": "goal alice@acme.com",
            "overall_task": "task alice@acme.com",
            "what_happened": "the call came back unusable",
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
