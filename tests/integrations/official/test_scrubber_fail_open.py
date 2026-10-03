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
