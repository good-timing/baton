"""The MCP install path against ``baton-spec``'s fake collector, in every
scenario of ``collector/shutdown.json``; what each checks is the file's ``about``."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import warnings
from typing import Any

import pytest

from baton._state import PROCESS_DROPS
from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.sinks import HttpSink
from tests._mcp_session import connected_session
from tests.functional.conftest import SPEC_ROOT
from tests.test_broken_collector import _received

_FILE = SPEC_ROOT / "collector" / "shutdown.json"
_SPEC: dict[str, Any] = json.loads(_FILE.read_text()) if _FILE.exists() else {"scenarios": []}
_NOT_RUN: dict[str, str] = {}
_TOOL_EVENTS = ("tool_call_start", "tool_call_end")


@pytest.mark.skipif(not _FILE.exists(), reason="baton-spec submodule not checked out")
@pytest.mark.parametrize(
    ("scenario", "collector"),
    [
        pytest.param(
            s,
            s,
            id=s["name"],
            marks=[pytest.mark.skip(reason=_NOT_RUN[s["name"]])] if s["name"] in _NOT_RUN else [],
        )
        for s in _SPEC["scenarios"]
    ],
    indirect=["collector"],
)
async def test_shutdown_keeps_its_promises(
    scenario: dict[str, Any], collector: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="baton")
    settings = {**_SPEC["settings"], **scenario.get("settings", {})}
    limits = _SPEC["limits"]
    close_limit = (settings["shutdown_budget_ms"] + limits["close_margin_ms"]) / 1000
    mcp = FastMCP("shutdown")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    sink = HttpSink(
        url=f"http://127.0.0.1:{collector}",
        api_key="k",
        request_timeout_seconds=settings["request_timeout_ms"] / 1000,
        shutdown_flush_timeout_seconds=settings["shutdown_budget_ms"] / 1000,
        backoff_base_seconds=settings["backoff_base_ms"] / 1000,
    )
    handle = install_baton(mcp, VendorConfig(vendor_id="v", vendor_display_name="V", sink=sink))
    before = PROCESS_DROPS.total()
    first_close: asyncio.Task[None] | None = None
    warned_from: tuple[int, int] | None = None

    with warnings.catch_warnings(record=True) as warned:
        warnings.simplefilter("always")

        async def closed(close: Any) -> None:
            nonlocal warned_from
            started = time.monotonic()
            await close
            assert time.monotonic() - started <= close_limit
            if warned_from is None:
                warned_from = (len(warned), len(caplog.records))

        async with connected_session(mcp) as session:
            for step in scenario["steps"]:
                if "call" in step:
                    for n in range(step["call"]):
                        started = time.monotonic()
                        result = await session.call_tool("echo", {"text": f"hi {n}"})
                        assert time.monotonic() - started <= limits["call_ms"] / 1000
                        assert result.content[0].text == f"hi {n}"  # type: ignore[union-attr]
                elif "flush" in step:
                    started = time.monotonic()
                    await handle.flush()
                    assert time.monotonic() - started <= close_limit
                elif "close" in step:
                    await closed(handle.aclose())
                elif "close_twice" in step:
                    first_close = asyncio.ensure_future(handle.aclose())
                    await asyncio.sleep(0)
                    await closed(handle.aclose())
                elif "check" in step:
                    events = _received(collector, scenario["mode"])["events"]
                    tool_events = [e for e in events if e["event_type"] in _TOOL_EVENTS]
                    assert len(tool_events) == step["check"]["tool_events"]
                else:
                    raise AssertionError(f"unknown step {step}")
            if first_close is not None:
                await first_close

    after = PROCESS_DROPS.total()
    grew = {
        r: after.get(r, 0) - before.get(r, 0) for r in after if after.get(r, 0) > before.get(r, 0)
    }
    expect = scenario["expect"]
    assert set(grew) == set(expect["dropped"]), grew
    for reason, least in expect["dropped"].items():
        assert grew[reason] >= least, grew
    assert not [r.getMessage() for r in caplog.records if r.exc_info]
    if "warnings_after_close" in expect:
        assert warned_from is not None
        said = [str(w.message) for w in warned[warned_from[0] :]]
        said += [
            r.getMessage() for r in caplog.records[warned_from[1] :] if r.levelno >= logging.WARNING
        ]
        assert len(said) == expect["warnings_after_close"], said
