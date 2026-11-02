"""The HTTP sink against ``baton-spec``'s fake collector, in every scenario of
``collector/scenarios.json``; what each checks is the file's ``about``."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from baton._state import PROCESS_DROPS
from baton.sinks import HttpSink
from tests.functional.conftest import SPEC_ROOT
from tests.test_sinks import _make_event

_COLLECTOR = SPEC_ROOT / "collector" / "fake_collector.py"
_FILE = SPEC_ROOT / "collector" / "scenarios.json"
_SPEC: dict[str, Any] = json.loads(_FILE.read_text()) if _FILE.exists() else {"scenarios": []}


@pytest.fixture
def collector(request: pytest.FixtureRequest) -> Iterator[int]:
    """The port of a collector in the scenario's mode, or 1, where nothing
    listens, for "down"."""
    scenario: dict[str, Any] = request.param
    if scenario["mode"] == "down":
        yield 1
        return
    process = subprocess.Popen(
        [
            sys.executable,
            str(_COLLECTOR),
            "--mode",
            scenario["mode"],
            "--delay-ms",
            str(scenario.get("delay_ms", 0)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        yield int(process.stdout.readline())
    finally:
        process.kill()
        process.wait()


def _received(port: int, mode: str, query: str = "") -> dict[str, Any]:
    if mode == "down":
        return {"accepted": [], "requests": 0, "reports": 0}
    url = f"http://127.0.0.1:{port}/_received{query}"
    with urllib.request.urlopen(url, timeout=5) as response:
        received: dict[str, Any] = json.load(response)
        return received


@pytest.mark.skipif(not _FILE.exists(), reason="baton-spec submodule not checked out")
@pytest.mark.parametrize(
    ("scenario", "collector"),
    [pytest.param(s, s, id=s["name"]) for s in _SPEC["scenarios"]],
    indirect=["collector"],
)
async def test_the_sink_keeps_its_promises(
    scenario: dict[str, Any], collector: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="baton")
    settings, limits = _SPEC["settings"], _SPEC["limits"]
    sink = HttpSink(
        url=f"http://127.0.0.1:{collector}",
        api_key="k",
        request_timeout_seconds=settings["request_timeout_ms"] / 1000,
        shutdown_flush_timeout_seconds=settings["shutdown_budget_ms"] / 1000,
        backoff_base_seconds=settings["backoff_base_ms"] / 1000,
    )
    sent = range(1, scenario["events"] + 1)
    for n in sent:
        started = time.monotonic()
        await sink.write(_make_event(sequence_number=n))
        assert time.monotonic() - started <= limits["write_ms"] / 1000
    expect = scenario.get("expect", {})
    if "reports" in expect:
        wait = f"?accepted={expect.get('accepted', 0)}&reports={expect['reports']}"
        wait += f"&wait_ms={limits['report_ms']}"
        await asyncio.to_thread(_received, collector, scenario["mode"], wait)
    started = time.monotonic()
    await sink.aclose()
    assert (
        time.monotonic() - started
        <= (settings["shutdown_budget_ms"] + limits["close_margin_ms"]) / 1000
    )

    received = _received(collector, scenario["mode"])
    accepted = received["accepted"]
    dropped = sum(PROCESS_DROPS.total().values())
    # A failure logged with a traceback is one that raised somewhere unseen.
    assert not [r.getMessage() for r in caplog.records if r.exc_info]
    assert scenario["mode"] == "down" or received["requests"] >= 1
    assert set(accepted) <= set(sent)
    assert len(accepted) + dropped >= len(sent)
    if "accepted" in expect:
        assert len(accepted) == expect["accepted"]
    if "dropped" in expect:
        assert dropped == expect["dropped"]
    if "reports" in expect:
        assert received["reports"] == expect["reports"]
