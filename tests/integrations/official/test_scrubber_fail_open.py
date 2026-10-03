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


async def _call(events_path: Path, scrubber: Any, *, fail: bool) -> dict[str, Any]:
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink
    from tests._mcp_session import connected_session

    mcp = FastMCP("failopen-official")

    @mcp.tool()
    def fetch(row: str) -> dict[str, Any]:
        if fail:
            raise ValueError(VENDOR_MSG)
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
    out: dict[str, Any] = {"ok": False, "error": None, "text": ""}
    try:
        async with connected_session(mcp) as client:
            try:
                res = await client.call_tool("fetch", {"row": "42"})
                out["ok"] = True
                out["text"] = str(getattr(res, "content", res))
            except Exception as exc:
                out["error"] = str(exc)
    finally:
        await handle.aclose()
    return out


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
