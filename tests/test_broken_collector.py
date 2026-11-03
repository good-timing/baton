"""The HTTP sink against ``baton-spec``'s fake collector, in every scenario of
``collector/scenarios.json``; what each checks is the file's ``about``."""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest

from baton._state import PROCESS_DROPS
from tests._fake_collector import each_scenario, from_baton, http_sink, received, scenario_file
from tests._http_fakes import make_event

_SPEC = scenario_file("scenarios.json")


@each_scenario(_SPEC)
async def test_the_sink_keeps_its_promises(
    scenario: dict[str, Any], collector: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="baton")
    settings = {**_SPEC["settings"], **scenario.get("settings", {})}
    limits = _SPEC["limits"]
    sink = http_sink(collector, settings)
    sent = range(1, scenario["events"] + 1)
    for n in sent:
        started = time.monotonic()
        await sink.write(make_event(sequence_number=n))
        assert time.monotonic() - started <= limits["write_ms"] / 1000
    expect = scenario.get("expect", {})
    if "reports" in expect:
        wait = f"?accepted={expect.get('accepted', 0)}&reports={expect['reports']}"
        wait += f"&wait_ms={limits['report_ms']}"
        await received(collector, scenario["mode"], wait)
    started = time.monotonic()
    await sink.aclose()
    assert (
        time.monotonic() - started
        <= (settings["shutdown_budget_ms"] + limits["close_margin_ms"]) / 1000
    )
    dropped = sum(PROCESS_DROPS.total().values())

    got = await received(collector, scenario["mode"])
    accepted = got["accepted"]
    # A failure logged with a traceback is one that raised somewhere unseen.
    assert not [r.getMessage() for r in from_baton(caplog.records) if r.exc_info]
    assert scenario["mode"] == "down" or got["requests"] >= 1
    assert set(accepted) <= set(sent)
    assert len(accepted) + dropped >= len(sent)
    if "accepted" in expect:
        assert len(accepted) == expect["accepted"]
    if "dropped" in expect:
        assert dropped == expect["dropped"]
    if "reports" in expect:
        assert got["reports"] == expect["reports"]
    if "requests" in expect:
        assert got["requests"] == expect["requests"]
