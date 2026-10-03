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
