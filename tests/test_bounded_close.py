"""SPEC §11.2 rule 7: an explicit flush or close returns within the shutdown
budget and never raises a sink's error, on every door a vendor can call."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

import baton.sinks as sinks_module
from baton import AsyncClient, Client
from baton._state import PROCESS_DROPS
from baton.events import Event
from baton.integrations._handle import BatonHandle
from baton.sinks import (
    HttpSink,
    MultiSink,
    Sink,
    aclose_within,
    bounded_sink_call,
    shutdown_budget_seconds,
)
from tests._http_fakes import (
    RELEASE,
    FakePool,
    fake_sink,
    hold_first,
    make_event,
    never_answers,
    pool_of,
    until,
)


class _NeverAnswers(Sink):
    async def write(self, event: Event) -> None:
        pass

    async def flush(self) -> None:
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        await asyncio.Event().wait()


class _NothingAnswers(_NeverAnswers):
    async def write(self, event: Event) -> None:
        await asyncio.Event().wait()


class _OnlyWriteHangs(Sink):
    async def write(self, event: Event) -> None:
        await asyncio.Event().wait()

    async def flush(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class _Raises(Sink):
    async def write(self, event: Event) -> None:
        pass

    async def flush(self) -> None:
        raise OSError("collector gone")

    async def aclose(self) -> None:
        raise OSError("collector gone")


class _PlainMethods(Sink):
    """A vendor sink written without ``async``: its methods return no awaitable."""

    async def write(self, event: Event) -> None:
        pass

    def flush(self) -> None:  # type: ignore[override]
        pass

    def aclose(self) -> None:  # type: ignore[override]
        pass


def _handle(sink: Sink) -> BatonHandle:
    return BatonHandle(sink=sink, annotation_tool_name="", vendor_id="v", session_id="s")


_ASYNC_DOORS: dict[str, Callable[[Sink], Callable[[], Awaitable[None]]]] = {
    "BatonHandle.flush": lambda sink: _handle(sink).flush,
    "BatonHandle.aclose": lambda sink: _handle(sink).aclose,
    "AsyncClient.flush": lambda sink: AsyncClient(sink=sink, vendor_id="v").flush,
    "AsyncClient.aclose": lambda sink: AsyncClient(sink=sink, vendor_id="v").aclose,
}


@pytest.fixture(autouse=True)
async def _cancel_left_running() -> AsyncIterator[None]:
    yield
    for task in list(sinks_module._left_running):
        task.cancel()


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(sinks_module, "SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 0.2)
    return 0.2


@pytest.mark.parametrize("door", sorted(_ASYNC_DOORS))
async def test_an_async_door_returns_from_a_sink_that_never_answers(
    door: str, short_budget: float, caplog: pytest.LogCaptureFixture
) -> None:
    call = _ASYNC_DOORS[door](_NeverAnswers())
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="baton"):
        await call()
    assert time.monotonic() - started < short_budget + 0.5
    assert any("left unfinished" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("door", sorted(_ASYNC_DOORS))
@pytest.mark.parametrize("sink_type", [_Raises, _PlainMethods])
async def test_an_async_door_never_raises(
    door: str, sink_type: type[Sink], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="baton"):
        await _ASYNC_DOORS[door](sink_type())()
    failed = [r for r in caplog.records if r.getMessage().endswith("failed")]
    assert len(failed) == (1 if sink_type is _Raises else 0)


@pytest.fixture
def stuck_sink() -> HttpSink:
    return _stuck_http_sink(budget=0.1)


class TestHttpSinkOnACollectorThatNeverAnswers:
    async def test_flush_returns_within_its_budget_then_at_once_and_aclose_stops_it(
        self, stuck_sink: HttpSink, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = stuck_sink
        await sink.write(make_event())
        with caplog.at_level(logging.WARNING, logger="baton"):
            started = time.monotonic()
            await sink.flush()
            assert time.monotonic() - started < 0.5
            second = time.monotonic()
            await sink.flush()
            await sink.flush()
            assert time.monotonic() - second < 0.05
            await sink.aclose()
        assert sink._sender.stopping
        messages = [r.getMessage() for r in caplog.records]
        assert sum("goes on in the background" in m for m in messages) == 1
        assert [m for m in messages if "flush() skipped" in m] == [
            "baton: flush() skipped: an earlier drain is still stuck; 1 event(s) wait to be sent."
        ]
        assert "baton: the sink closed with 1 event(s) unsent." in messages

    async def test_overlapping_acloses_run_one_close_and_the_second_waits(
        self, stuck_sink: HttpSink, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = stuck_sink
        await sink.write(make_event())
        with caplog.at_level(logging.WARNING, logger="baton"):
            first = asyncio.ensure_future(sink.aclose())
            await asyncio.sleep(0)
            await sink.aclose()
            assert first.done()
        assert sum("closed with" in r.getMessage() for r in caplog.records) == 1

    async def test_flush_waits_again_once_the_stalled_drain_has_finished(self) -> None:
        release = threading.Event()
        sink = fake_sink(hold_first(release), shutdown_flush_timeout_seconds=0.05)
        await sink.write(make_event())
        await sink.flush()
        release.set()
        await _until_unstalled(sink)
        await sink.write(make_event(sequence_number=2))
        await sink.flush()
        assert sink._sender.pending() == 0
        await sink.aclose()


async def _until_unstalled(sink: HttpSink) -> None:
    await until(lambda: sink._sender.stalled is None or sink._sender.stalled.done())


async def test_each_stall_warns_once_when_a_flush_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first_answer = threading.Event()
    sink = fake_sink(
        hold_first(first_answer, then=never_answers), shutdown_flush_timeout_seconds=0.05
    )
    with caplog.at_level(logging.WARNING, logger="baton"):
        await sink.write(make_event())
        await sink.flush()
        await sink.flush()
        first_answer.set()
        await _until_unstalled(sink)
        await sink.write(make_event(sequence_number=2))
        await sink.flush()
        await sink.flush()
        await sink.aclose()
    assert sum("flush() skipped" in r.getMessage() for r in caplog.records) == 2


@pytest.mark.parametrize("budget", [float("inf"), float("nan"), -1, "5", True, 1e10, 10**400])
def test_a_bad_budget_falls_back_to_the_default_with_a_warning(
    budget: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="baton"):
        sink = HttpSink(
            url="http://collector.example", api_key="k", shutdown_flush_timeout_seconds=budget
        )
    assert sink._shutdown_flush_timeout == sinks_module.SHUTDOWN_FLUSH_TIMEOUT_SECONDS
    assert any("shutdown_flush_timeout_seconds" in r.getMessage() for r in caplog.records)


def test_a_budget_of_zero_is_kept() -> None:
    sink = HttpSink(url="http://collector.example", api_key="k", shutdown_flush_timeout_seconds=0)
    assert sink._shutdown_flush_timeout == 0


def test_a_multisink_gets_the_longest_budget_of_its_sinks() -> None:
    long = HttpSink(url="http://collector.example", api_key="k", shutdown_flush_timeout_seconds=10)
    assert shutdown_budget_seconds(MultiSink([_Raises(), long])) == 10


async def test_bounded_sink_call_waits_as_long_as_an_http_sinks_own_budget(
    short_budget: float,
) -> None:
    class _SlowFlush(HttpSink):
        async def flush(self) -> None:
            await asyncio.sleep(0.6)

    sink = _SlowFlush(url="http://collector.example", api_key="k", shutdown_flush_timeout_seconds=1)
    started = time.monotonic()
    await bounded_sink_call(sink, "flush")
    assert time.monotonic() - started >= 0.6


class _HttpSinkSubclass(HttpSink):
    pass


def test_the_sync_client_close_reports_what_a_stuck_http_sink_left_unsent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Client(sink=_stuck_http_sink(budget=0.1), vendor_id="v")
    with client.trace(tool_name="t") as trace:
        trace.observed(result="ok")
    with caplog.at_level(logging.WARNING, logger="baton"):
        client.close()
    assert any(
        "closed with" in r.getMessage() and "unsent" in r.getMessage() for r in caplog.records
    )


async def test_aclose_stops_the_sending_thread(stuck_sink: HttpSink) -> None:
    await stuck_sink.write(make_event())
    await stuck_sink.flush()
    thread = stuck_sink._sender.thread
    assert thread is not None and thread.is_alive()
    await stuck_sink.aclose()
    RELEASE.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(pool_of(stuck_sink).requests) == 1, "no retry starts after close"


async def test_no_event_is_sent_after_close_gives_up(caplog: pytest.LogCaptureFixture) -> None:
    held = threading.Event()
    singles: list[int] = []

    def old_collector(path: str, body: Any) -> int:
        if path.endswith("/batch"):
            return 404
        singles.append(body["sequence_number"])
        if len(singles) == 2:
            held.wait()
        return 202

    sink = fake_sink(old_collector, shutdown_flush_timeout_seconds=0.1)
    for n in (1, 2, 3):
        await sink.write(make_event(sequence_number=n))
    await sink.aclose()
    thread = sink._sender.thread
    assert thread is not None
    held.set()
    thread.join(timeout=2)
    assert singles == [1, 2]
    assert PROCESS_DROPS.total() == {"unsent_at_close": 2}


async def test_a_close_cancelled_from_outside_is_finished_by_the_next_aclose(
    stuck_sink: HttpSink,
) -> None:
    await stuck_sink.write(make_event())
    first = asyncio.ensure_future(stuck_sink.aclose())
    await asyncio.sleep(0)
    assert stuck_sink._closing is not None
    stuck_sink._closing.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await stuck_sink.aclose()
    assert stuck_sink._closed


async def test_work_that_fails_after_its_limit_is_logged_as_a_warning(
    short_budget: float, caplog: pytest.LogCaptureFixture
) -> None:
    class _FailsLate(_Raises):
        async def flush(self) -> None:
            await asyncio.sleep(short_budget + 0.4)
            raise OSError("collector gone")

    with caplog.at_level(logging.WARNING, logger="baton"):
        await bounded_sink_call(_FailsLate(), "flush")
        await asyncio.sleep(0.5)
    failed = [r for r in caplog.records if r.getMessage() == "baton: flush() failed"]
    assert len(failed) == 1 and failed[0].levelno == logging.WARNING


async def test_an_async_client_close_cancelled_from_outside_is_finished_by_the_next_aclose(
    short_budget: float,
) -> None:
    client = AsyncClient(sink=_NeverAnswers(), vendor_id="v")
    first = asyncio.ensure_future(client.aclose())
    await asyncio.sleep(0)
    assert client._closing is not None
    client._closing.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await client.aclose()
    assert client._closing.done() and not client._closing.cancelled()


async def test_the_exit_flush_gives_a_stuck_drain_its_budget(stuck_sink: HttpSink) -> None:
    await stuck_sink.write(make_event())
    await stuck_sink.flush()
    started = time.monotonic()
    stuck_sink._atexit_flush()
    assert time.monotonic() - started >= 0.09
    await stuck_sink.aclose()


def test_a_multisink_that_holds_itself_gets_the_default_budget() -> None:
    inner: list[Sink] = [_Raises()]
    sink = MultiSink(inner)
    inner.append(sink)
    assert shutdown_budget_seconds(sink) == sinks_module.SHUTDOWN_FLUSH_TIMEOUT_SECONDS


def _stuck_http_sink(budget: float, sink_type: type[HttpSink] = HttpSink) -> HttpSink:
    return sink_type(
        url="http://collector.example",
        api_key="k",
        shutdown_flush_timeout_seconds=budget,
        _http_client=FakePool(never_answers),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("wrap", [False, True])
async def test_aclose_within_hands_the_time_left_to_our_sinks(
    wrap: bool, caplog: pytest.LogCaptureFixture
) -> None:
    http = _stuck_http_sink(budget=30)
    http._sender.enqueue(make_event())
    sink: Sink = MultiSink([http]) if wrap else http
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="baton"):
        await aclose_within(sink, 0.1)
    assert time.monotonic() - started < 1.0
    assert any("closed with 1 event(s) unsent" in r.getMessage() for r in caplog.records)


async def test_aclose_within_calls_a_subclass_aclose_without_the_time_left() -> None:
    closed = []

    class _OwnClose(HttpSink):
        async def aclose(self) -> None:  # type: ignore[override]
            closed.append(True)

    await aclose_within(_OwnClose(url="http://collector.example", api_key="k"), 0.1)
    assert closed == [True]


def test_the_sync_client_close_waits_for_writes_within_the_budget_not_on_top(
    short_budget: float, monkeypatch: pytest.MonkeyPatch
) -> None:

    client = Client(sink=_NothingAnswers(), vendor_id="v")
    with client.trace(tool_name="t") as trace:
        trace.observed(result="ok")
    started = time.monotonic()
    client.close()
    assert time.monotonic() - started < short_budget + 0.5 + 0.3


def test_the_sync_client_close_delivers_a_slow_write_and_closes_the_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sinks_module, "SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 1.0)

    class _Slow(Sink):
        def __init__(self) -> None:
            self.written: list[Event] = []
            self.closed = False

        async def write(self, event: Event) -> None:
            await asyncio.sleep(0.15)
            self.written.append(event)

        async def flush(self) -> None:
            pass

        async def aclose(self) -> None:
            await asyncio.sleep(0.15)
            self.closed = True

    sink = _Slow()
    client = Client(sink=sink, vendor_id="v")
    with client.trace(tool_name="t") as trace:
        trace.observed(result="ok")
    client.close()
    assert sink.closed
    assert len(sink.written) == 2


def test_the_sync_client_close_does_not_wait_on_a_sink_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sinks_module, "SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 1.0)

    class _Batching(Sink):
        def __init__(self) -> None:
            self.batch: list[Event] = []
            self.sent: list[Event] = []
            self.ticker: asyncio.Task[None] | None = None

        async def _tick(self) -> None:
            await asyncio.Event().wait()

        async def write(self, event: Event) -> None:
            self.ticker = self.ticker or asyncio.create_task(self._tick())
            self.batch.append(event)

        async def flush(self) -> None:
            pass

        async def aclose(self) -> None:
            assert self.ticker is not None
            self.ticker.cancel()
            await asyncio.sleep(0.1)
            self.sent, self.batch = self.batch, []

    sink = _Batching()
    client = Client(sink=sink, vendor_id="v")
    with client.trace(tool_name="t") as trace:
        trace.observed(result="ok")
    started = time.monotonic()
    client.close()
    assert time.monotonic() - started < 0.6
    assert len(sink.sent) == 2


async def test_aclose_within_never_extends_a_sink_budget() -> None:
    http = _stuck_http_sink(budget=0.1)
    http._sender.enqueue(make_event())
    started = time.monotonic()
    await aclose_within(http, 30)
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("http_type", [HttpSink, _HttpSinkSubclass])
def test_a_stuck_write_leaves_our_http_sink_the_time_left_and_its_count(
    http_type: type[HttpSink],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(sinks_module, "SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 0.1)
    # Over the 0.5s margin, so a fresh budget on top of the wait would not fit.
    http = _stuck_http_sink(budget=0.6, sink_type=http_type)
    client = Client(sink=MultiSink([_OnlyWriteHangs(), http]), vendor_id="v")
    with client.trace(tool_name="t") as trace:
        trace.observed(result="ok")
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="baton"):
        client.close()
    assert time.monotonic() - started < 1.0
    assert any("the sink closed with" in r.getMessage() for r in caplog.records)


def test_close_after_a_flush_that_gave_up_gives_the_drain_the_time_left() -> None:
    def slow(path: str, body: Any) -> int:
        time.sleep(0.4)
        return 200

    sink = fake_sink(slow, shutdown_flush_timeout_seconds=0.3)
    client = Client(sink=sink, vendor_id="v")
    with client.trace(tool_name="t") as trace:
        trace.observed(result="ok")
    client.flush()
    client.close()
    assert len(pool_of(sink).events()) == 2
    assert PROCESS_DROPS.total() == {}
