"""Every event is delivered or counted, by reason."""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, get_args

import pytest
import urllib3
from pytest_httpserver import HTTPServer

from baton import AsyncClient, Client
from baton._state import PROCESS_DROPS, DropReason
from baton.events import Event
from baton.sinks import (
    FileSink,
    HttpSink,
    MultiSink,
    Sink,
    SinkClosedError,
    StdoutSink,
    _BatchSender,
    build_or_none,
    safe_write,
)
from tests._http_fakes import Answerer, events_in, fake_sink, never_answers, pool_of
from tests.test_sinks import _make_event


def _http_sink(answer: Answerer, **kwargs: Any) -> HttpSink:
    return fake_sink(answer, max_retries=0, **kwargs)


def _answers(statuses: list[int]) -> Answerer:
    """Answers each request holding an event with the next status; a request
    holding only a ``client_report`` gets 202."""
    events = iter(statuses)

    def answer(path: str, body: Any) -> int:
        if all(e["event_type"] == "client_report" for e in events_in(path, body)):
            return 202
        return next(events)

    return answer


_never_answers = never_answers


class _WriteRaises(Sink):
    async def write(self, event: Event) -> None:
        raise OSError("disk full")

    async def flush(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


async def _buffer_full() -> None:
    sink = _http_sink(_never_answers, buffer_size=2)
    with pytest.warns(UserWarning):
        for _ in range(3):
            sink._enqueue_for_test(_make_event())


async def _refused() -> None:
    sink = _http_sink(lambda path, body: 400)
    await sink.write(_make_event())
    await sink.flush()


async def _encode_failed() -> None:
    def build() -> Event:
        raise ValueError("unbuildable")

    build_or_none(build, logging.getLogger("baton"))


async def _write_failed() -> None:
    await safe_write(_WriteRaises(), _make_event(), logging.getLogger("baton"))


async def _after_close() -> None:
    client = Client(sink=_WriteRaises(), vendor_id="v")
    client.close()
    client.annotate(what_happened="late")


async def _unsent_at_close() -> None:
    sink = _http_sink(_never_answers, shutdown_flush_timeout_seconds=0.05)
    sink._enqueue_for_test(_make_event())
    await sink.aclose()


async def _write_cancelled_at_close() -> None:
    class _WriteHangs(_WriteRaises):
        async def write(self, event: Event) -> None:
            await asyncio.Event().wait()

    client = Client(sink=_WriteHangs(), vendor_id="v")
    client.annotate(what_happened="stuck")
    client.close()


_SCENARIOS: dict[str, tuple[DropReason, Callable[[], Coroutine[Any, Any, None]]]] = {
    "buffer_full": ("buffer_full", _buffer_full),
    "refused": ("refused", _refused),
    "encode_failed": ("encode_failed", _encode_failed),
    "write_failed": ("write_failed", _write_failed),
    "after_close": ("after_close", _after_close),
    "unsent_at_close": ("unsent_at_close", _unsent_at_close),
    "write_cancelled_at_close": ("unsent_at_close", _write_cancelled_at_close),
}


def test_every_reason_has_a_scenario() -> None:
    assert {reason for reason, _ in _SCENARIOS.values()} == set(get_args(DropReason))


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_a_dropped_event_is_counted_once_under_its_reason(name: str) -> None:
    reason, scenario = _SCENARIOS[name]
    await scenario()
    assert PROCESS_DROPS.total() == {reason: 1}


@dataclass(eq=True)
class _Unhashable(Sink):
    closed: bool = False

    async def write(self, event: Event) -> None:
        if self.closed:
            raise SinkClosedError

    async def flush(self) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


@dataclass(eq=True, slots=True)
class _NoWeakref:
    """Duck-typed: any ``Sink`` subclass takes a weakref."""

    closed: bool = False

    async def write(self, event: Event) -> None:
        if self.closed:
            raise SinkClosedError

    async def flush(self) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("make", ["http", "file", "stdout", "multi", "unhashable", "no_weakref"])
async def test_a_write_after_the_sink_closed_is_counted_after_close_with_one_warning(
    make: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sinks: dict[str, Callable[[], Sink]] = {
        "http": lambda: _http_sink(lambda path, body: 202),
        "file": lambda: FileSink(tmp_path / "events.jsonl"),
        "stdout": StdoutSink,
        "multi": lambda: MultiSink([FileSink(tmp_path / "m.jsonl"), StdoutSink()]),
        "unhashable": _Unhashable,
        "no_weakref": lambda: cast(Sink, _NoWeakref()),
    }
    sink = sinks[make]()
    await sink.aclose()
    for _ in range(3):
        await safe_write(sink, _make_event(), logging.getLogger("baton"))
    assert PROCESS_DROPS.total() == {"after_close": 3}
    assert [(r.levelno, r.exc_info) for r in caplog.records if r.name == "baton"] == [
        (logging.WARNING, None)
    ]


async def test_a_closed_sink_beside_a_cancelled_one_is_still_counted() -> None:
    class _Cancelled(_WriteRaises):
        async def write(self, event: Event) -> None:
            raise asyncio.CancelledError

    sink = MultiSink([_Unhashable(closed=True), _Cancelled()])
    await safe_write(sink, _make_event(), logging.getLogger("baton"))
    assert PROCESS_DROPS.total() == {"after_close": 1}


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_a_library_snapshot_to_a_closed_sink_is_counted_once_and_not_retried(
    asynchronous: bool, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sink = FileSink(tmp_path / "events.jsonl")
    await sink.aclose()
    tools = [{"name": "fetch", "inputSchema": {"type": "object"}}]
    if asynchronous:
        aclient = AsyncClient(sink=sink, vendor_id="v")
        for _ in range(2):
            await aclient.list_tools(tools)
    else:
        client = Client(sink=sink, vendor_id="v")
        for _ in range(2):
            await asyncio.to_thread(client.list_tools, tools)
        await asyncio.to_thread(client.close)
    assert PROCESS_DROPS.total() == {"after_close": 1}
    baton = [r for r in caplog.records if r.name.startswith("baton")]
    assert [(r.levelno, r.exc_info) for r in baton] == [(logging.WARNING, None)]


async def test_a_multisink_with_one_closed_sink_still_delivers_and_counts_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    closed = FileSink(tmp_path / "closed.jsonl")
    await closed.aclose()
    sink = MultiSink([closed, FileSink(tmp_path / "open.jsonl")])
    await safe_write(sink, _make_event(), logging.getLogger("baton"))
    assert PROCESS_DROPS.total() == {}
    assert (tmp_path / "open.jsonl").read_text().count("\n") == 1
    assert [r for r in caplog.records if r.name.startswith("baton")] == []


async def test_the_first_delivery_after_a_loss_says_what_was_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _http_sink(_answers([400, 202, 202]))
    with caplog.at_level(logging.WARNING, logger="baton"):
        for _ in range(3):
            await sink.write(_make_event())
            await sink.flush()
    recovered = [r.getMessage() for r in caplog.records if "delivering again" in r.getMessage()]
    assert recovered == ["baton: delivering again; 1 event(s) lost before this: refused 1."]


async def test_a_healthy_run_counts_and_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    sink = _http_sink(lambda path, body: 202)
    with caplog.at_level(logging.WARNING, logger="baton"):
        for _ in range(3):
            await sink.write(_make_event())
        await sink.aclose()
    assert PROCESS_DROPS.total() == {}
    assert caplog.records == []


def test_an_event_after_the_bridge_stopped_is_counted() -> None:
    client = Client(sink=_WriteRaises(), vendor_id="v")
    assert client._bridge is not None
    client._bridge.stop()
    client.annotate(what_happened="lost")
    assert PROCESS_DROPS.total() == {"write_failed": 1}
    client.close()


def test_the_exit_flush_warns_about_what_the_collector_did_not_take_and_counts_nothing(
    httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
) -> None:
    """SPEC §11.2 rule 8: nothing can send or log a count taken at exit."""
    httpserver.expect_request("/v0/events/batch").respond_with_data(status=503)
    sink = HttpSink(url=httpserver.url_for(""), api_key="k", backoff_base_seconds=0.001)
    sink._enqueue_for_test(_make_event())
    with caplog.at_level(logging.WARNING, logger="baton"):
        sink._atexit_flush()
    assert PROCESS_DROPS.total() == {}
    assert any("1 event(s) still buffered and lost" in m for m in caplog.messages)
    sink._closed = True


def test_the_exit_flush_warns_and_counts_nothing_when_the_circuit_is_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _http_sink(_never_answers, circuit_breaker_threshold=1)
    sink._enqueue_for_test(_make_event())
    sink._circuit.record_failure()
    with caplog.at_level(logging.WARNING, logger="baton"):
        sink._atexit_flush()
    assert PROCESS_DROPS.total() == {}
    assert any("process exiting with 1 buffered event(s)" in m for m in caplog.messages)
    sink._closed = True


def test_an_exit_flush_counts_nothing_and_the_close_after_it_counts_once() -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sink = _http_sink(_never_answers, shutdown_flush_timeout_seconds=0.05)
    asyncio.run_coroutine_threadsafe(sink.write(_make_event()), loop).result(timeout=1)
    sink._atexit_flush()
    assert PROCESS_DROPS.total() == {}
    asyncio.run_coroutine_threadsafe(sink.aclose(), loop).result(timeout=1)
    assert PROCESS_DROPS.total() == {"unsent_at_close": 1}
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=1)


def test_an_exit_flush_on_a_running_loop_warns_about_what_it_left(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sink = _http_sink(_never_answers, circuit_breaker_threshold=1)
    sink._circuit.record_failure()
    asyncio.run_coroutine_threadsafe(sink.write(_make_event()), loop).result(timeout=1)
    with caplog.at_level(logging.WARNING, logger="baton"):
        sink._atexit_flush()
    assert any("process exiting with 1 buffered event(s)" in m for m in caplog.messages)
    assert PROCESS_DROPS.total() == {}
    sink._closed = True
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=1)


def test_an_overflow_warning_raised_as_an_error_counts_the_event_once() -> None:
    sink = _http_sink(_never_answers, buffer_size=1)
    sink._enqueue_for_test(_make_event())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        asyncio.run(safe_write(sink, _make_event(), logging.getLogger("baton")))
    assert PROCESS_DROPS.total() == {"write_failed": 1}
    sink._closed = True


def test_a_failed_exit_flush_warns_and_counts_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no network at exit")

    monkeypatch.setattr(_BatchSender, "send_inline", broken)
    sink = _http_sink(_never_answers)
    sink._enqueue_for_test(_make_event())
    with caplog.at_level(logging.WARNING, logger="baton"):
        sink._atexit_flush()
    assert PROCESS_DROPS.total() == {}
    assert any(
        "best-effort shutdown flush failed; 1 event(s) still buffered" in m for m in caplog.messages
    )
    sink._closed = True


async def test_steady_loss_beside_delivery_says_so_once_a_minute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _http_sink(_answers([400, 202, 400, 202]))
    with caplog.at_level(logging.WARNING, logger="baton"):
        for _ in range(4):
            await sink.write(_make_event())
            await sink.flush()
    assert sum("delivering again" in r.getMessage() for r in caplog.records) == 1


async def test_an_event_pushed_out_while_being_sent_does_not_cost_the_next_one() -> None:
    gate = threading.Event()

    def answer(path: str, body: Any) -> int:
        gate.wait()
        return 202

    sink = _http_sink(answer, buffer_size=2, flush_interval_seconds=0)
    for n in (1, 2, 3):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            await sink.write(_make_event(sequence_number=n))
        await asyncio.sleep(0.01)  # event 1 is being sent when 3 pushes it out
    gate.set()
    await sink.aclose()
    events = [e for e in pool_of(sink).events() if e["event_type"] != "client_report"]
    assert [e["sequence_number"] for e in events] == [1, 2, 3]
    assert PROCESS_DROPS.total() == {"buffer_full": 1}


def test_a_network_failure_keeps_the_events_and_counts_nothing() -> None:
    def down(path: str, body: Any) -> int:
        raise urllib3.exceptions.ProtocolError("connection reset")

    sink = _http_sink(down, shutdown_flush_timeout_seconds=0.2)
    sink._enqueue_for_test(_make_event())
    asyncio.run(sink.flush())
    assert len(sink._buffer) == 1
    assert PROCESS_DROPS.total() == {}
    asyncio.run(sink.aclose())
    assert PROCESS_DROPS.total() == {"unsent_at_close": 1}
