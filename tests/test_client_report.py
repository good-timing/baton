"""HttpSink sends this process's loss totals after a delivery (SPEC §11.4.7)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

import pytest
import urllib3

import baton._state as state_module
import baton._uuid as uuid_module
import baton.sinks as sinks_module
from baton._state import PROCESS_DROPS, PROCESS_SEQUENCE
from baton.events import ToolCallStartPayload
from baton.sinks import (
    _BATCH_CLOSE,
    _BATCH_OPEN,
    BATCH_MAX_BYTES,
    BATCH_MAX_EVENTS,
    HttpSink,
    _BatchSender,
    _encode,
)
from tests._http_fakes import (
    Answer,
    ReportCollector,
    child_answers,
    events_in,
    fake_sink,
    make_event,
    never_answers,
    until,
)


async def _send(sink: HttpSink, n: int) -> None:
    for _ in range(n):
        await sink.write(make_event())
        await sink.flush()


@pytest.fixture
def no_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_BatchSender, "REPORT_INTERVAL_SECONDS", 0.0)


async def test_a_delivery_after_a_loss_sends_the_totals() -> None:
    collector = ReportCollector([400, 202])
    sink = fake_sink(collector)
    await _send(sink, 2)
    [report] = collector.reports
    delivered = make_event()
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
    collector = ReportCollector([202, 202, 202])
    sink = fake_sink(collector)
    await _send(sink, 3)
    await sink.aclose()
    assert collector.reports == []


async def test_at_most_one_report_a_minute() -> None:
    collector = ReportCollector([400, 202, 400, 202])
    sink = fake_sink(collector)
    await _send(sink, 4)
    assert len(collector.reports) == 1
    await sink.aclose()


async def test_a_new_loss_is_reported_once_the_interval_passed(no_interval: None) -> None:
    collector = ReportCollector([400, 202, 202, 400, 202])
    sink = fake_sink(collector)
    await _send(sink, 5)
    assert [r["payload"]["dropped"] for r in collector.reports] == [{"refused": 1}, {"refused": 2}]
    await sink.aclose()


@pytest.mark.parametrize("status", [500, 401, 400, 422])
async def test_a_failed_report_is_not_counted_and_is_sent_again(
    no_interval: None, status: int
) -> None:
    collector = ReportCollector([400, 202, 202], report_status=status)
    sink = fake_sink(collector)
    await _send(sink, 3)
    assert len(collector.reports) == 2
    assert PROCESS_DROPS.total() == {"refused": 1}
    await sink.aclose()


@pytest.mark.parametrize("first", [503, 413])
async def test_a_kept_batch_keeps_its_report(first: int) -> None:
    """Even when a 413 split leaves the report alone in the request that fails."""
    in_delivery, gate = threading.Event(), threading.Event()
    statuses = iter([400, 202])
    report_answers = [first, 503, 202] if first == 413 else [first, 202]
    answers = iter(report_answers)
    reports: list[str] = []

    def answer(path: str, body: Any) -> int:
        events = events_in(path, body)
        if events[-1]["event_type"] != "client_report":
            if not reports:
                status = next(statuses)
                if status == 202:
                    in_delivery.set()
                    gate.wait()
                return status
            return 202
        reports.append(events[-1]["event_id"])
        return next(answers)

    sink = fake_sink(answer, backoff_base_seconds=0.01, flush_interval_seconds=0.01)
    await _send(sink, 1)
    await sink.write(make_event())
    flushing = asyncio.ensure_future(sink.flush())
    assert await asyncio.to_thread(in_delivery.wait, 3)
    await sink.write(make_event())
    gate.set()
    await flushing
    await until(lambda: len(reports) == len(report_answers))
    assert len(set(reports)) == 1
    await sink.aclose()


@pytest.mark.parametrize("report_status", [503, None], ids=["5xx", "no-answer"])
async def test_lone_reports_that_fail_do_not_hold_up_the_events(
    no_interval: None, report_status: int | None
) -> None:
    threshold = 5
    answered = 0

    def answer(path: str, body: Any) -> int:
        nonlocal answered
        if path.endswith("/batch"):
            return 404
        if body["event_type"] == "client_report":
            if report_status is None:
                raise urllib3.exceptions.ProtocolError("down")
            return report_status
        answered += 1
        return 400 if answered == 1 else 202

    sink = fake_sink(
        answer,
        circuit_breaker_threshold=threshold,
        backoff_base_seconds=0.01,
        flush_interval_seconds=0.01,
    )
    await _send(sink, 2)
    # Time for a kept report to be retried past the threshold.
    await asyncio.sleep(0.2)
    await _send(sink, 2 * threshold - 2)
    assert answered == 2 * threshold
    assert sink._circuit.can_request()
    await sink.aclose()


@pytest.mark.parametrize("status", [400, 422])
async def test_a_collector_that_refuses_a_report_sent_alone_gets_no_more(
    no_interval: None, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    collector = ReportCollector([400, 202, 400, 202], report_status=status)

    def no_batch_endpoint(path: str, body: Any) -> int | Answer:
        return 404 if path.endswith("/batch") else collector(path, body)

    sink = fake_sink(no_batch_endpoint)
    await _send(sink, 4)
    assert len(collector.reports) == 1
    assert PROCESS_DROPS.total() == {"refused": 2}
    assert sum("REFUSED" in r.getMessage() for r in caplog.records) == 1
    await sink.aclose()


async def test_a_report_that_cannot_reach_the_collector_costs_nothing(
    no_interval: None, caplog: pytest.LogCaptureFixture
) -> None:
    collector = ReportCollector([400, 202, 202, 202], report_status=None)
    sink = fake_sink(collector)
    await _send(sink, 4)
    await sink.aclose()
    assert collector.delivered == 3
    assert PROCESS_DROPS.total() == {"refused": 1}
    assert [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info] == []


async def test_no_report_starts_once_close_began(no_interval: None) -> None:
    collector = ReportCollector([400])
    sink = fake_sink(collector)
    await _send(sink, 1)
    await sink.aclose()
    sink._sender._maybe_report_locked(make_event())
    assert sink._sender.report is None


async def test_a_report_that_hangs_holds_close_to_its_budget_and_is_not_counted(
    no_interval: None,
) -> None:
    statuses = iter([400, 202, 400, 202])
    reports = 0

    def answer(path: str, body: Any) -> int:
        nonlocal reports
        if all(e["event_type"] == "client_report" for e in events_in(path, body)):
            reports += 1
            return never_answers(path, body)
        return next(statuses)

    sink = fake_sink(answer, shutdown_flush_timeout_seconds=0.2)
    for _ in range(4):
        await sink.write(make_event())
        await sink.flush()
    assert reports == 1, "one request at a time, so the hung report holds the rest"
    loop = asyncio.get_running_loop()
    started = loop.time()
    await sink.aclose()
    assert loop.time() - started < 1.0
    assert PROCESS_DROPS.total() == {"refused": 1, "unsent_at_close": 2}


@pytest.mark.parametrize(
    ("code", "reports_sent"), [("payload_malformed", 1), ("quota_exhausted", 3)]
)
async def test_only_payload_malformed_in_the_batch_results_stops_reports(
    no_interval: None, code: str, reports_sent: int
) -> None:
    sent_reports = 0
    statuses = iter(["refused", "accepted", "refused", "accepted", "accepted"])

    def answer(path: str, body: Any) -> Answer:
        nonlocal sent_reports
        results = []
        for event in events_in(path, body):
            if event["event_type"] == "client_report":
                sent_reports += 1
                status = "refused"
            else:
                status = next(statuses)
            results.append(
                {"event_id": event["event_id"], "status": status, "error": {"code": code}}
            )
        return Answer(200, json.dumps({"results": results}).encode())

    sink = fake_sink(answer)
    await _send(sink, 5)
    assert sent_reports == reports_sent
    assert PROCESS_DROPS.total() == {"refused": 2}
    await sink.aclose()


async def test_a_batch_refused_whole_with_the_report_in_it_does_not_stop_reports(
    no_interval: None,
) -> None:
    in_delivery = threading.Event()
    gate = threading.Event()
    statuses = iter([400, 202, 400, 202, 202])
    batches: list[list[str]] = []

    def answer(path: str, body: Any) -> Answer:
        batches.append([e["event_type"] for e in events_in(path, body)])
        status = next(statuses)
        if len(batches) == 2:
            in_delivery.set()
            gate.wait()
        return Answer(status, b"payload_malformed: bad batch" if status == 400 else b"")

    sink = fake_sink(answer)
    await _send(sink, 1)
    await sink.write(make_event())
    flushing = asyncio.ensure_future(sink.flush())
    await asyncio.to_thread(in_delivery.wait, 3)
    await sink.write(make_event())
    gate.set()
    await flushing
    await _send(sink, 1)
    assert batches[2] == ["tool_call_start", "client_report"]
    assert batches[-1] == ["client_report"]
    assert PROCESS_DROPS.total() == {"refused": 2}
    await sink.aclose()


def _event_filling_half_a_batch() -> Any:
    """Two of these fill a batch's bytes to within a few bytes of the cap."""
    event = make_event()
    room = (BATCH_MAX_BYTES - len(_BATCH_OPEN) - len(_BATCH_CLOSE) - 1) // 2
    blob = room - len(_encode(event)) - len(',"params":{"blob":""}') - 5
    return event.model_copy(
        update={"payload": ToolCallStartPayload(tool_name="t", params={"blob": "x" * blob})}
    )


@pytest.mark.parametrize(
    ("make", "full", "load"),
    [(make_event, BATCH_MAX_EVENTS, 3 * BATCH_MAX_EVENTS), (_event_filling_half_a_batch, 2, 6)],
    ids=["count-full", "bytes-full"],
)
async def test_a_report_goes_while_every_batch_is_full(
    no_interval: None, make: Callable[[], Any], full: int, load: int
) -> None:
    gate = threading.Event()
    batches: list[list[str]] = []

    def answer(path: str, body: Any) -> int:
        batches.append([e["event_type"] for e in events_in(path, body)])
        if len(batches) == 2:
            gate.wait()
        return 400 if len(batches) == 1 else 202

    sink = fake_sink(answer)
    await _send(sink, 1)
    for _ in range(load):
        await sink.write(make())
    flushing = asyncio.ensure_future(sink.flush())
    gate.set()
    await flushing
    assert max(len(b) for b in batches) == full
    assert len([b for b in batches if "client_report" in b and "tool_call_start" in b]) == 1
    await sink.aclose()


@pytest.mark.parametrize(
    ("whole_batch", "refused"), [(True, 2), (False, 1)], ids=["whole-batch-422", "per-event"]
)
async def test_a_report_answered_after_close_let_go_of_it_is_not_counted(
    whole_batch: bool, refused: int, caplog: pytest.LogCaptureFixture
) -> None:
    in_delivery, delivery_gate = threading.Event(), threading.Event()
    in_report, report_gate = threading.Event(), threading.Event()
    statuses = iter([400, 202])

    def answer(path: str, body: Any) -> int | Answer:
        events = events_in(path, body)
        if events[-1]["event_type"] != "client_report":
            status = next(statuses)
            if status == 202:
                in_delivery.set()
                delivery_gate.wait()
            return status
        in_report.set()
        report_gate.wait()
        if whole_batch:
            return 422
        results = [
            {"event_id": e["event_id"], "status": "refused", "error": {"code": "x"}}
            if e["event_type"] == "client_report"
            else {"event_id": e["event_id"], "status": "accepted"}
            for e in events
        ]
        return Answer(200, json.dumps({"results": results}).encode())

    sink = fake_sink(answer)
    await _send(sink, 1)
    await sink.write(make_event())
    flushing = asyncio.ensure_future(sink.flush())
    assert await asyncio.to_thread(in_delivery.wait, 3)
    await sink.write(make_event())
    delivery_gate.set()
    assert await asyncio.to_thread(in_report.wait, 3)
    closing = asyncio.ensure_future(sink.aclose())
    await until(lambda: sink._sender.reports_stopped)
    report_gate.set()
    await asyncio.gather(flushing, closing)
    assert PROCESS_DROPS.total() == {"refused": refused}
    logged = [r.getMessage() for r in caplog.records if "REFUSED" in r.getMessage()]
    assert all("HTTP 400" in m or (whole_batch and "422" in m) for m in logged), logged


def test_no_report_after_the_exit_flush(no_interval: None) -> None:
    collector = ReportCollector([400, 202])
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sink = fake_sink(collector)
    asyncio.run_coroutine_threadsafe(sink.write(make_event()), loop).result(timeout=1)
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
    answer = child_answers(
        lambda: PROCESS_DROPS.instance_id != parent_id and PROCESS_DROPS.total() == {}
    )
    assert answer == b"1"
    assert PROCESS_DROPS.instance_id == parent_id


async def test_a_forked_child_reports_its_own_loss_even_when_it_matches_the_parents() -> None:
    collector = ReportCollector([400, 202, 400, 202])
    sink = fake_sink(collector)
    await _send(sink, 2)
    state_module._after_fork_in_child()
    sinks_module._reset_senders_in_child()
    await _send(sink, 2)
    assert [r["payload"]["dropped"] for r in collector.reports] == [{"refused": 1}] * 2
    await sink.aclose()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_forked_child_never_waits_on_the_parents_sequence_lock() -> None:
    with PROCESS_SEQUENCE._lock:
        answer = child_answers(lambda: PROCESS_SEQUENCE.next() > 0)
    assert answer == b"1"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_forked_child_never_waits_on_the_parents_uuid_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is what Python < 3.14 runs, ``_state``'s fork hook included."""
    monkeypatch.setattr(state_module, "uuid7", uuid_module._uuid7_fallback)
    with uuid_module._lock:
        answer = child_answers(lambda: uuid_module._uuid7_fallback().version == 7)
    assert answer == b"1"
