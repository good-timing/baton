"""HttpSink sends this process's loss totals after a delivery (SPEC §11.4.7)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from baton._state import PROCESS_DROPS
from baton.sinks import HttpSink
from tests.test_drop_counts import _http_sink as _sink
from tests.test_sinks import _make_event


class _Collector:
    """Answers events by a list of statuses, and reports with ``report_status``."""

    def __init__(self, event_statuses: list[int], report_status: int | None = 202) -> None:
        """``report_status=None`` makes a report fail to connect."""
        self._event_statuses: Iterator[int] = iter(event_statuses)
        self.report_status = report_status
        self.reports: list[dict[str, Any]] = []
        self.delivered = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["event_type"] == "client_report":
            self.reports.append(body)
            if self.report_status is None:
                raise httpx.ConnectError("down")
            return httpx.Response(self.report_status)
        status = next(self._event_statuses)
        self.delivered += status == 202
        return httpx.Response(status)


async def _send(sink: HttpSink, n: int) -> None:
    for _ in range(n):
        await sink.write(_make_event())
        await sink.flush()
        if sink._report_task is not None:
            await sink._report_task


@pytest.fixture
def no_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HttpSink, "_REPORT_INTERVAL_SECONDS", 0.0)


async def test_a_delivery_after_a_loss_sends_the_totals() -> None:
    collector = _Collector([400, 202])
    sink = _sink(collector)
    await _send(sink, 2)
    [report] = collector.reports
    delivered = _make_event()
    assert report["payload"] == {
        "instance_id": str(PROCESS_DROPS.instance_id),
        "dropped": {"refused": 1},
    }
    assert report["session_id"] == str(PROCESS_DROPS.instance_id)
    assert (report["tenant_id"], report["vendor_id"], report["consent_token"]) == (
        delivered.tenant_id,
        delivered.vendor_id,
        delivered.consent_token,
    )
    for absent in ("call_id", "principal", "client_observed", "transport_observed"):
        assert report.get(absent) is None
    await sink.aclose()


async def test_a_healthy_run_sends_no_report() -> None:
    collector = _Collector([202, 202, 202])
    sink = _sink(collector)
    await _send(sink, 3)
    await sink.aclose()
    assert collector.reports == []


async def test_at_most_one_report_a_minute() -> None:
    collector = _Collector([400, 202, 400, 202])
    sink = _sink(collector)
    await _send(sink, 4)
    assert len(collector.reports) == 1
    await sink.aclose()


async def test_a_new_loss_is_reported_once_the_interval_passed(no_interval: None) -> None:
    collector = _Collector([400, 202, 202, 400, 202])
    sink = _sink(collector)
    await _send(sink, 5)
    assert [r["payload"]["dropped"] for r in collector.reports] == [{"refused": 1}, {"refused": 2}]
    await sink.aclose()


@pytest.mark.parametrize("status", [500, 401])
async def test_a_failed_report_is_not_counted_and_is_sent_again(
    no_interval: None, status: int
) -> None:
    collector = _Collector([400, 202, 202], report_status=status)
    sink = _sink(collector)
    await _send(sink, 3)
    assert len(collector.reports) == 2
    assert PROCESS_DROPS.total() == {"refused": 1}
    await sink.aclose()


@pytest.mark.parametrize("status", [400, 422])
async def test_a_collector_that_refuses_reports_gets_no_more(
    no_interval: None, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    collector = _Collector([400, 202, 400, 202], report_status=status)
    sink = _sink(collector)
    await _send(sink, 4)
    assert len(collector.reports) == 1
    assert PROCESS_DROPS.total() == {"refused": 2}
    assert sum("REFUSED" in r.getMessage() for r in caplog.records) == 2
    await sink.aclose()


async def test_a_report_that_cannot_reach_the_collector_costs_nothing(
    no_interval: None, caplog: pytest.LogCaptureFixture
) -> None:
    collector = _Collector([400, 202, 202, 202], report_status=None)
    sink = _sink(collector)
    await _send(sink, 4)
    await sink.aclose()
    assert collector.delivered == 3
    assert PROCESS_DROPS.total() == {"refused": 1}
    assert [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info] == []


async def test_no_report_starts_once_close_began(no_interval: None) -> None:
    collector = _Collector([400])
    sink = _sink(collector)
    await _send(sink, 1)
    await sink.aclose()
    sink._maybe_report(_make_event())
    assert sink._report_task is None


async def test_one_report_at_a_time_and_close_cancels_it(no_interval: None) -> None:
    hang = asyncio.Event()
    statuses = iter([400, 202, 400, 202])
    reports = 0

    async def answer(request: httpx.Request) -> httpx.Response:
        nonlocal reports
        if json.loads(request.content)["event_type"] == "client_report":
            reports += 1
            await hang.wait()
        return httpx.Response(next(statuses))

    sink = _sink(answer, shutdown_flush_timeout_seconds=5.0)
    for _ in range(4):
        await sink.write(_make_event())
        await sink.flush()
        await asyncio.sleep(0)
    assert reports == 1
    assert sink._report_task is not None and not sink._report_task.done()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await sink.aclose()
    assert loop.time() - started < 1.0
    await asyncio.sleep(0)
    assert sink._report_task.cancelled()
    assert PROCESS_DROPS.total() == {"refused": 2}


def test_no_report_after_the_exit_flush(no_interval: None) -> None:
    collector = _Collector([400, 202])
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sink = _sink(collector)
    asyncio.run_coroutine_threadsafe(sink.write(_make_event()), loop).result(timeout=1)
    asyncio.run_coroutine_threadsafe(sink.flush(), loop).result(timeout=1)
    sink._atexit_flush()
    asyncio.run_coroutine_threadsafe(_send(sink, 1), loop).result(timeout=1)
    assert collector.delivered == 1
    assert collector.reports == []
    asyncio.run_coroutine_threadsafe(sink.aclose(), loop).result(timeout=1)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=1)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_forked_child_reports_under_its_own_id_and_counts() -> None:
    PROCESS_DROPS.add("refused")
    parent_id = PROCESS_DROPS.instance_id
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        ok = PROCESS_DROPS.instance_id != parent_id and PROCESS_DROPS.total() == {}
        os.write(write, b"1" if ok else b"0")
        os._exit(0)
    os.close(write)
    os.waitpid(pid, 0)
    assert os.read(read, 1) == b"1"
    assert PROCESS_DROPS.instance_id == parent_id
