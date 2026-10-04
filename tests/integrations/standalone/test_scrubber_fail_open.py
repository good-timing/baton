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


async def _call(events_path: Path, scrubber: Any, *, fail: bool) -> dict[str, Any]:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.sinks import FileSink

    mcp: Any = FastMCP("failopen")

    @mcp.tool
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
    out: dict[str, Any] = {"ok": False, "error": None}
    try:
        async with Client(mcp) as client:
            try:
                await client.call_tool("fetch", {"row": "42"})
                out["ok"] = True
            except Exception as exc:
                out["error"] = str(exc)
    finally:
        await handle.aclose()
    return out


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
    """`scrub_or_none` at `runtime_adapter._clean` — fires on EVERY call."""
    thrower = _Thrower(CLIENT_NAME)
    res = await _call(tmp_path / "p1.jsonl", thrower, fail=False)
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert res["ok"], f"fail-open broken: {res['error']}"


async def test_a_throwing_scrubber_does_not_replace_the_vendors_error(
    tmp_path: Path,
) -> None:
    """`safe_emit` on the RETURN shape — the scrubber runs inside the thunk.

    ⚠ The site is `_error_result.py`, NOT the middleware's `except`: FastMCP
    converts a raising tool to a returned-flag result before the middleware
    sees it, and hands the scrubber its own wrapper text.
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


class _DeepThrower:
    """Identity, except where `trip` appears anywhere in the value's repr.

    `end_result_fields` hands the scrubber the WHOLE jsonable result in one
    call, so a str-only thrower never fires on a dict-returning tool.
    """

    def __init__(self, trip: str) -> None:
        self._trip = trip
        self.tripped = 0

    def __call__(self, value: Any) -> Any:
        if self._trip in repr(value):
            self.tripped += 1
            raise RuntimeError("scrubber exploded")
        return value


async def test_a_throwing_scrubber_on_the_END_leg_does_not_break_the_call(
    tmp_path: Path,
) -> None:
    """`end_result_fields` — a statement before `safe_emit` until 10-03."""
    thrower = _DeepThrower(RESULT_MARKER)
    res = await _call(tmp_path / "p3.jsonl", thrower, fail=False)
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert res["ok"], f"fail-open broken on the END leg: {res['error']}"


async def _call_soft_fail(events_path: Path, scrubber: Any) -> dict[str, Any]:
    """A tool that RETURNS the error flag instead of raising — SPEC §11.4.3(2).

    A raising tool does NOT reach `returned_error_fields` on this surface: it
    propagates through `call_next` as `ToolError` and lands in the middleware's
    `except`. Only a returned flag reaches the RETURN projection.
    """
    import mcp.types as mcp_types
    from fastmcp import Client, FastMCP

    try:  # fastmcp 3.x / 4.x
        from fastmcp.tools import ToolResult
    except ImportError:  # 2.14.7 — the floor exports it from the leaf module
        from fastmcp.tools.tool import ToolResult

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.sinks import FileSink

    mcp: Any = FastMCP("failopen-soft")

    @mcp.tool
    def soft_fail(row: str) -> Any:
        return ToolResult(
            content=[mcp_types.TextContent(type="text", text=RETURN_REASON)],
            is_error=True,
        )

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
    out: dict[str, Any] = {"ok": False, "error": None}
    try:
        async with Client(mcp) as client:
            try:
                await client.call_tool("soft_fail", {"row": "42"})
                out["ok"] = True
            except Exception as exc:
                out["error"] = str(exc)
    finally:
        await handle.aclose()
    return out


async def test_control_a_returned_error_flag_reaches_the_client(tmp_path: Path) -> None:
    """Without this the RETURN-leg test below cannot distinguish a guard from
    a rig whose tool never returned the flag."""
    res = await _call_soft_fail(tmp_path / "c3.jsonl", lambda v: v)
    assert res["ok"] or RETURN_REASON in (res["error"] or ""), res


async def test_a_throwing_scrubber_on_the_RETURN_leg_does_not_break_the_call(
    tmp_path: Path,
) -> None:
    """`returned_error_fields` — a statement before `safe_emit` until 10-03."""
    thrower = _DeepThrower(RETURN_REASON)
    res = await _call_soft_fail(tmp_path / "p4.jsonl", thrower)
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert res["ok"] or RETURN_REASON in (res["error"] or ""), (
        f"fail-open broken on the RETURN leg: {res['error']}"
    )


async def _call_annotate(events_path: Path, scrubber: Any) -> dict[str, Any]:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.sinks import FileSink

    mcp: Any = FastMCP("failopen-annotate")

    @mcp.tool
    def fetch(row: str) -> dict[str, Any]:
        return {"record": "ok"}

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
    out: dict[str, Any] = {"ok": False, "error": None}
    try:
        async with Client(mcp) as client:
            try:
                await client.call_tool(
                    handle.annotation_tool_name,
                    {"user_goal": GOAL_MARKER, "signal_type": "failure"},
                )
                out["ok"] = True
            except Exception as exc:
                out["error"] = str(exc)
    finally:
        await handle.aclose()
    return out


async def test_control_the_annotation_tool_accepts_a_goal(tmp_path: Path) -> None:
    res = await _call_annotate(tmp_path / "c4.jsonl", lambda v: v)
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
    res = await _call_annotate(tmp_path / "p5.jsonl", thrower)
    assert thrower.tripped, "the probe never reached its target; the test proves nothing"
    assert res["ok"], f"fail-open broken on the annotation path: {res['error']}"
