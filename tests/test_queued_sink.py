"""Every sink but ``HttpSink`` is written from Baton's own thread (SPEC §11.2
rules 3, 7 and 8), so a slow or failing sink never slows a tool call."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import warnings
from collections.abc import Callable

import pytest

from baton import AsyncClient
from baton._state import PROCESS_DROPS
from baton.events import Event
from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.sinks import QUEUE_BOUND, HttpSink, Sink, StdoutSink, queued, vendor_sink
from tests._event_helpers import holding_sink_queue
from tests._http_fakes import make_event

needs_a_write_before_any_flush = pytest.mark.skipif(
    holding_sink_queue(),
    reason="HOLD_SINK_QUEUE holds every write until a flush",
)


class _Recording(Sink):
    def __init__(
        self,
        *,
        write_delay: float = 0.0,
        close_delay: float = 0.0,
        fail: bool = False,
        gate: threading.Event | None = None,
    ) -> None:
        self.events: list[Event] = []
        self.calls: list[str] = []
        self.write_delay = write_delay
        self.close_delay = close_delay
        self.fail = fail
        self.gate = gate

    async def write(self, event: Event) -> None:
        if self.gate is not None:
            await asyncio.to_thread(self.gate.wait)
        await asyncio.sleep(self.write_delay)
        if self.fail:
            raise OSError("disk full")
        self.events.append(event)
        self.calls.append("write")

    async def flush(self) -> None:
        self.calls.append("flush")

    async def aclose(self) -> None:
        await asyncio.sleep(self.close_delay)
        self.calls.append("aclose")


async def _until(done: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 2
    while not done():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.005)


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch) -> float:
    import baton.sinks as sinks_module

    monkeypatch.setattr(sinks_module, "SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 0.4)
    return 0.4


async def test_a_slow_sink_does_not_slow_a_traced_call() -> None:
    sink = _Recording(write_delay=0.5)
    client = AsyncClient(sink=sink, vendor_id="v")
    started = time.monotonic()
    async with client.trace(tool_name="fetch") as t:
        t.observed(result="ok")
    assert time.monotonic() - started < 0.2
    await client.aclose()
    assert [e.event_type for e in sink.events] == ["tool_call_start", "tool_call_end"]


async def test_a_slow_sink_does_not_slow_an_mcp_tool_call() -> None:
    sink = _Recording(write_delay=0.5)
    mcp = FastMCP("srv")
    handle = install_baton(mcp, VendorConfig(vendor_id="v", vendor_display_name="V", sink=sink))

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    started = time.monotonic()
    await mcp.call_tool("echo", {"text": "x"})
    assert time.monotonic() - started < 0.2
    assert handle.sink is sink
    await handle.aclose()
    assert "tool_call_end" in [e.event_type for e in sink.events]


def test_only_a_sink_whose_write_does_not_only_queue_is_wrapped() -> None:
    http = HttpSink("http://127.0.0.1:9", api_key="k")
    assert queued(http) is http
    wrapped = queued(StdoutSink())
    assert queued(wrapped) is wrapped
    assert isinstance(vendor_sink(wrapped), StdoutSink)


async def test_a_full_queue_drops_the_oldest_counts_it_and_warns_once() -> None:
    gate = threading.Event()
    sink = _Recording(gate=gate)
    q = queued(sink)
    try:
        await q.write(make_event(0))
        await _until(lambda: q._sender.in_flight == 1)  # type: ignore[attr-defined]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for n in range(1, QUEUE_BOUND + 3):
                await q.write(make_event(n))
        assert PROCESS_DROPS.total() == {"buffer_full": 2}
        assert len([w for w in caught if "buffer full" in str(w.message)]) == 1
    finally:
        gate.set()
    await q.aclose()
    numbers = [e.sequence_number for e in sink.events]
    assert numbers[:3] == [0, 3, 4]
    assert len(numbers) == QUEUE_BOUND + 1


async def test_a_failing_sink_counts_each_event_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    q = queued(_Recording(fail=True))
    with caplog.at_level(logging.WARNING, logger="baton"):
        for n in range(3):
            await q.write(make_event(n))
        await q.flush()
    assert PROCESS_DROPS.total() == {"write_failed": 3}
    assert len([r for r in caplog.records if "failed to write" in r.getMessage()]) == 1


async def test_flush_waits_for_the_events_written_before_it() -> None:
    sink = _Recording(write_delay=0.05)
    q = queued(sink)
    for n in range(3):
        await q.write(make_event(n))
    await q.flush()
    assert sink.calls == ["write", "write", "write", "flush"]


async def test_close_writes_what_is_queued_then_closes_the_sink() -> None:
    sink = _Recording(write_delay=0.05)
    q = queued(sink)
    for n in range(3):
        await q.write(make_event(n))
    await q.aclose()
    assert sink.calls == ["write", "write", "write", "aclose"]
    assert PROCESS_DROPS.total() == {}


async def test_an_event_after_close_is_counted_by_the_caller() -> None:
    from baton.sinks import safe_write

    q = queued(_Recording())
    await q.aclose()
    await safe_write(q, make_event(), logging.getLogger("baton"))
    assert PROCESS_DROPS.total() == {"after_close": 1}


async def test_close_on_a_stuck_sink_returns_within_its_budget_and_counts_the_rest(
    short_budget: float, caplog: pytest.LogCaptureFixture
) -> None:
    gate = threading.Event()
    q = queued(_Recording(gate=gate))
    try:
        for n in range(3):
            await q.write(make_event(n))
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="baton"):
            await q.aclose()
        assert time.monotonic() - started < short_budget + 0.2
    finally:
        gate.set()
    assert PROCESS_DROPS.total() == {"unsent_at_close": 3}
    assert "baton: the sink closed with 3 event(s) unsent." in [
        r.getMessage() for r in caplog.records
    ]


async def test_a_second_close_waits_for_the_first_and_the_sink_closes_once() -> None:
    sink = _Recording(close_delay=0.1)
    q = queued(sink)
    await q.write(make_event())
    first = asyncio.ensure_future(q.aclose())
    await asyncio.sleep(0)
    await q.aclose()
    assert first.done()
    assert sink.calls == ["write", "aclose"]


async def test_at_exit_the_queue_is_written_out() -> None:
    gate = threading.Event()
    sink = _Recording(gate=gate)
    q = queued(sink)
    await q.write(make_event())
    threading.Timer(0.05, gate.set).start()
    q._sender.flush_at_exit()  # type: ignore[attr-defined]
    assert sink.calls == ["write", "flush"]
    await q.aclose()


async def test_the_sink_gets_the_event_as_it_was_when_recorded() -> None:
    from baton.scrub import identity_scrub

    sink = _Recording(write_delay=0.1)
    client = AsyncClient(sink=sink, vendor_id="v", scrubber=identity_scrub)
    rows = {"rows": ["alice"]}
    async with client.trace(tool_name="fetch", params={"q": rows}) as t:
        t.observed(result=rows)
    rows["rows"].append("LATE")
    await client.aclose()
    end = next(e for e in sink.events if e.event_type == "tool_call_end")
    assert end.payload.result == {"rows": ["alice"]}


async def test_an_event_recorded_while_close_drains_is_written(short_budget: float) -> None:
    sink = _Recording(write_delay=0.02)
    q = queued(sink)
    await q.write(make_event(0))
    closing = asyncio.ensure_future(q.aclose())
    await asyncio.sleep(0.005)
    await q.write(make_event(1))
    await closing
    assert [e.sequence_number for e in sink.events] == [0, 1]
    assert PROCESS_DROPS.total() == {}


async def test_a_flush_on_a_stuck_sink_waits_its_budget_once(short_budget: float) -> None:
    gate = threading.Event()
    q = queued(_Recording(gate=gate))
    try:
        await q.write(make_event())
        await q.flush()
        started = time.monotonic()
        await q.flush()
        assert time.monotonic() - started < 0.05
    finally:
        gate.set()
    await q.aclose()


async def test_a_sink_never_written_to_is_closed_without_a_thread() -> None:
    sink = _Recording()
    q = queued(sink)
    await q.flush()
    await q.aclose()
    assert sink.calls == ["flush", "aclose"]
    assert not q._sender.started()  # type: ignore[attr-defined]


@needs_a_write_before_any_flush
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
async def test_a_drain_that_died_is_started_again() -> None:
    class _ExitsOnce(_Recording):
        async def write(self, event: Event) -> None:
            if not self.calls:
                self.calls.append("exit")
                raise SystemExit
            await super().write(event)

    sink = _ExitsOnce()
    q = queued(sink)
    await q.write(make_event(0))
    sender = q._sender  # type: ignore[attr-defined]
    await _until(lambda: not sender.thread.is_alive())
    await q.write(make_event(1))
    started = time.monotonic()
    await q.aclose()
    assert time.monotonic() - started < 0.5
    assert [e.sequence_number for e in sink.events] == [1]
    assert PROCESS_DROPS.total() == {"write_failed": 1}


@needs_a_write_before_any_flush
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
async def test_a_flush_after_the_drain_died_writes_what_waits() -> None:
    gate = threading.Event()

    class _ExitsOnce(_Recording):
        async def write(self, event: Event) -> None:
            if not self.calls:
                self.calls.append("exit")
                await asyncio.to_thread(gate.wait)
                raise SystemExit
            await super().write(event)

    sink = _ExitsOnce()
    q = queued(sink)
    for n in range(3):
        await q.write(make_event(n))
    sender = q._sender  # type: ignore[attr-defined]
    gate.set()
    await _until(lambda: not sender.thread.is_alive())
    await q.flush()
    assert [e.sequence_number for e in sink.events] == [1, 2]
    await q.aclose()


async def test_an_overflow_warning_made_an_error_still_drops_the_oldest() -> None:
    gate = threading.Event()
    sink = _Recording(gate=gate)
    q = queued(sink)
    try:
        await q.write(make_event(0))
        await _until(lambda: q._sender.in_flight == 1)  # type: ignore[attr-defined]
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for n in range(1, QUEUE_BOUND + 3):
                await q.write(make_event(n))
    finally:
        gate.set()
    await q.aclose()
    assert PROCESS_DROPS.total() == {"buffer_full": 2}
    assert [e.sequence_number for e in sink.events][:2] == [0, 3]


async def test_at_exit_a_stuck_sink_holds_exit_for_its_budget_only(
    short_budget: float, caplog: pytest.LogCaptureFixture
) -> None:
    gate = threading.Event()
    q = queued(_Recording(gate=gate))
    try:
        await q.write(make_event())
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="baton"):
            q._sender.flush_at_exit()  # type: ignore[attr-defined]
        assert time.monotonic() - started < short_budget + 0.2
        assert any("not written" in r.getMessage() for r in caplog.records)
    finally:
        gate.set()
    await q.aclose()
