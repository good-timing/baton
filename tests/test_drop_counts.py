"""Every event is delivered or counted, by reason."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import warnings
from collections.abc import Callable, Coroutine
from typing import Any, get_args

import httpx
import pytest
from pytest_httpserver import HTTPServer

from baton import Client
from baton._state import PROCESS_DROPS, DropReason
from baton.events import Event
from baton.sinks import HttpSink, Sink, build_or_none, safe_write
from tests.test_sinks import _make_event


def _http_sink(answer: Callable[[httpx.Request], Any], **kwargs: Any) -> HttpSink:
    return HttpSink(
        url="http://collector.example",
        api_key="k",
        max_retries=0,
        _http_client=httpx.AsyncClient(transport=httpx.MockTransport(answer)),
        **kwargs,
    )


def _answers(statuses: list[int]) -> Callable[[httpx.Request], httpx.Response]:
    """Answers each event with the next status; a ``client_report`` gets 202."""
    events = iter(statuses)

    def answer(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["event_type"] == "client_report":
            return httpx.Response(202)
        return httpx.Response(next(events))

    return answer


async def _never_answers(request: httpx.Request) -> httpx.Response:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


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
    sink = _http_sink(lambda request: httpx.Response(400))
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


async def _unsent_at_exit() -> None:
    sink = _http_sink(_never_answers, circuit_breaker_threshold=1)
    sink._enqueue_for_test(_make_event())
    sink._circuit.record_failure()
    sink._atexit_flush()
    sink._closed = True


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
    "unsent_at_exit": ("unsent_at_close", _unsent_at_exit),
    "write_cancelled_at_close": ("unsent_at_close", _write_cancelled_at_close),
}


def test_every_reason_has_a_scenario() -> None:
    assert {reason for reason, _ in _SCENARIOS.values()} == set(get_args(DropReason))


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_a_dropped_event_is_counted_once_under_its_reason(name: str) -> None:
    reason, scenario = _SCENARIOS[name]
    await scenario()
    assert PROCESS_DROPS.total() == {reason: 1}


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
    sink = _http_sink(lambda request: httpx.Response(202))
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


def test_the_exit_flush_counts_what_the_collector_did_not_take(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v0/events").respond_with_data(status=503)
    sink = HttpSink(url=httpserver.url_for(""), api_key="k")
    sink._enqueue_for_test(_make_event())
    sink._atexit_flush()
    assert PROCESS_DROPS.total() == {"unsent_at_close": 1}
    sink._closed = True


def test_an_exit_flush_then_close_counts_each_lost_event_once() -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    sink = _http_sink(_never_answers, shutdown_flush_timeout_seconds=0.05)
    asyncio.run_coroutine_threadsafe(sink.write(_make_event()), loop).result(timeout=1)
    sink._atexit_flush()
    asyncio.run_coroutine_threadsafe(sink.aclose(), loop).result(timeout=1)
    assert PROCESS_DROPS.total() == {"unsent_at_close": 1}
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


def test_the_exit_flush_counts_what_is_left_when_it_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no network at exit")

    monkeypatch.setattr(httpx, "Client", broken)
    sink = _http_sink(_never_answers)
    sink._enqueue_for_test(_make_event())
    sink._atexit_flush()
    assert PROCESS_DROPS.total() == {"unsent_at_close": 1}
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
    posted: list[int] = []
    gate = asyncio.Event()

    async def answer(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content)["sequence_number"])
        await gate.wait()
        return httpx.Response(202)

    sink = _http_sink(answer, buffer_size=2)
    for n in (1, 2, 3):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            await sink.write(_make_event(sequence_number=n))
        await asyncio.sleep(0.01)  # event 1 is being sent when 3 pushes it out
    gate.set()
    await sink.aclose()
    assert posted == [1, 2, 3]
    assert PROCESS_DROPS.total() == {"buffer_full": 1}
