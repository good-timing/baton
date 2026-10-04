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

try:  # fastmcp 3.x / 4.x
    from fastmcp.tools import ToolResult as _ToolResult
except ImportError:  # 2.14.7 — the floor exports it only from the leaf module
    from fastmcp.tools.tool import ToolResult as _ToolResult

# ⚠ `fastmcp` 2.14.7 is the `fastmcp-matrix` FLOOR (`ci.yml`) and its
# `ToolResult.__init__` takes no `is_error` at all, so the RETURN-leg probes
# below cannot even construct their fixture there — `TypeError`, not a failed
# assertion. Measured: without this guard that leg is `2 failed, 7 passed`.
# Same guard, same reason, as `test_iserror_reclassify.py`, which also pins the
# absence positively so the day the floor grows the field it reddens instead of
# skipping forever.
FLAG_IS_EXPRESSIBLE = "is_error" in getattr(_ToolResult, "model_fields", {}) or hasattr(
    _ToolResult, "is_error"
)
_needs_flag = pytest.mark.skipif(
    not FLAG_IS_EXPRESSIBLE,
    reason="fastmcp floor (2.14.7) cannot express a returned error flag; "
    "test_iserror_reclassify.py::test_floor_has_no_flag_to_read pins that absence",
)


class _DeepThrower:
    """Identity, except where `trip` appears anywhere in the value's repr.

    `end_result_fields` hands the scrubber the WHOLE jsonable result in one
    call, so a str-only thrower never fires on a dict-returning tool.

    ⚠ Records WHAT it tripped on. `tripped` alone cannot tell the two
    projections apart: both scrub a value whose repr contains the reason, so a
    RETURN-leg probe passes unchanged when only `end_result_fields` ran — which
    is exactly what happens wherever the error flag is unreadable. The RETURN
    projection scrubs `error_text(result)`, a BARE STRING; the END projection
    scrubs the serialised envelope. The type of the tripped value is the
    discriminator.
    """

    def __init__(self, trip: str) -> None:
        self._trip = trip
        self.tripped = 0
        self.tripped_on: Any = None

    def __call__(self, value: Any) -> Any:
        if self._trip in repr(value):
            self.tripped += 1
            self.tripped_on = value
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


@_needs_flag
async def test_control_a_returned_error_flag_reaches_the_client(tmp_path: Path) -> None:
    """Without this the RETURN-leg test below cannot distinguish a guard from
    a rig whose tool never returned the flag."""
    res = await _call_soft_fail(tmp_path / "c3.jsonl", lambda v: v)
    assert res["ok"] or RETURN_REASON in (res["error"] or ""), res


@_needs_flag
async def test_a_throwing_scrubber_on_the_RETURN_leg_does_not_break_the_call(
    tmp_path: Path,
) -> None:
    """`returned_error_fields` — a statement before `safe_emit` until 10-03."""
    thrower = _DeepThrower(RETURN_REASON)
    res = await _call_soft_fail(tmp_path / "p4.jsonl", thrower)
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


async def _annotate_with(events_path: Path, scrubber: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """Drive `_annotate` and return the events it wrote."""
    import json

    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.sinks import FileSink

    mcp: Any = FastMCP("annotate-scrub")

    @mcp.tool
    def fetch(row: str) -> dict[str, Any]:
        return {"record": "ok"}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="scrub",
            vendor_display_name="Scrub",
            consent_token="ct_scrub",
            sink=FileSink(str(events_path)),
            tenant_id="t-scrub",
            scrubber=scrubber,
        ),
    )
    try:
        async with Client(mcp) as client:
            await client.call_tool(handle.annotation_tool_name, kwargs)
    finally:
        await handle.aclose()
    return [json.loads(line) for line in events_path.read_text().splitlines() if line]


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
    events = await _annotate_with(
        tmp_path / "w1.jsonl",
        lambda v: v.replace("alice@acme.com", "[REDACTED]") if isinstance(v, str) else v,
        user_goal="goal alice@acme.com",
        overall_task="task alice@acme.com",
        signal_type="failure",
    )
    ann = [e for e in events if e.get("event_type") == "annotation"]
    assert ann, f"no annotation event written: {events!r}"
    payload = ann[0]["payload"]
    # The control: the field that was ALREADY scrubbed. Without it a scrubber
    # the rig never wired up reads exactly like a working one.
    assert payload["intent"] == "goal [REDACTED]", payload
    assert payload["workflow"] == "task [REDACTED]", (
        f"`workflow` bypassed the vendor's scrubber: {payload['workflow']!r}"
    )
