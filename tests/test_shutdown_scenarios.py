"""The MCP install path against ``baton-spec``'s fake collector, in every
scenario of ``collector/shutdown.json``; what each checks is the file's ``about``."""

from __future__ import annotations

import asyncio
import logging
import time
import warnings
from collections.abc import Awaitable
from typing import Any

import pytest

from baton._state import PROCESS_DROPS
from tests._fake_collector import (
    each_scenario,
    from_baton,
    http_sink,
    install_echo,
    received,
    scenario_file,
)
from tests._mcp_session import connected_session

_SPEC = scenario_file("shutdown.json")
_TOOL_EVENTS = ("tool_call_start", "tool_call_end")


@each_scenario(_SPEC)
async def test_shutdown_keeps_its_promises(
    scenario: dict[str, Any], collector: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="baton")
    settings = {**_SPEC["settings"], **scenario.get("settings", {})}
    limits = _SPEC["limits"]
    close_limit = (settings["shutdown_budget_ms"] + limits["close_margin_ms"]) / 1000
    mcp, handle = install_echo(http_sink(collector, settings))
    still_closing: set[asyncio.Future[None]] = set()
    warned_from: tuple[int, int] | None = None

    async def within_limit(work: Awaitable[Any]) -> None:
        started = time.monotonic()
        await work
        assert time.monotonic() - started <= close_limit

    with warnings.catch_warnings(record=True) as warned:
        warnings.simplefilter("always")

        def closed() -> None:
            nonlocal warned_from
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
                    await within_limit(handle.flush())
                elif "close" in step:
                    await within_limit(handle.aclose())
                    closed()
                elif "close_twice" in step:
                    closes = {asyncio.ensure_future(handle.aclose()) for _ in range(2)}
                    first = asyncio.wait(closes, return_when=asyncio.FIRST_COMPLETED)
                    await within_limit(first)
                    still_closing = {c for c in closes if not c.done()}
                    closed()
                elif "check" in step:
                    events = (await received(collector, scenario["mode"]))["events"]
                    tool_events = [e for e in events if e["event_type"] in _TOOL_EVENTS]
                    assert len(tool_events) == step["check"]["tool_events"]
                else:
                    raise AssertionError(f"unknown step {step}")
            await within_limit(asyncio.gather(*still_closing))

    dropped = PROCESS_DROPS.total()
    expect = scenario["expect"]
    assert set(dropped) == set(expect["dropped"]), dropped
    for reason, least in expect["dropped"].items():
        assert dropped[reason] >= least, dropped
    assert not [r.getMessage() for r in from_baton(caplog.records) if r.exc_info]
    if "warnings_after_close" in expect:
        assert warned_from is not None
        said = [str(w.message) for w in warned[warned_from[0] :]]
        said += [r.getMessage() for r in from_baton(caplog.records[warned_from[1] :])]
        assert len(said) == expect["warnings_after_close"], said
