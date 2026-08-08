"""Tests for the HttpSink per SPEC §11.2 + CHARTER ADR-4.

Coverage: basic POST to /v0/events; bearer auth; JSON body shape; bounded
buffer with overflow + UserWarning (emitted once); retry-with-backoff on
transient failure; circuit breaker after consecutive failures; flush() drains;
aclose() flushes + closes.

Uses pytest-httpserver for a real in-process HTTP server (no mocks).
"""

from __future__ import annotations

import asyncio
import warnings
from datetime import UTC, datetime
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.events import ToolCallStartEvent, ToolCallStartPayload
from baton.sinks import HttpSink


def _make_event(sequence_number: int = 1) -> ToolCallStartEvent:
    return ToolCallStartEvent(
        tenant_id="ten_test",
        vendor_id="ten_test",
        session_id="sess_test",
        sequence_number=sequence_number,
        captured_at=datetime.now(UTC),
        consent_token="ct_test",
        agent_runtime="claude-code",
        payload=ToolCallStartPayload(tool_name="t"),
    )


# =============================================================================
# Basic POST to ingest endpoint
# =============================================================================


class TestEmitPostsToIngest:
    async def test_event_posted_to_v0_events(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        sink = HttpSink(url=httpserver.url_for(""), api_key="test-key")
        await sink.write(_make_event())
        await sink.flush()
        await sink.aclose()
        httpserver.check_assertions()

    async def test_bearer_auth_header(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(
            "/v0/events",
            method="POST",
            headers={"Authorization": "Bearer my-secret-key"},
        ).respond_with_data("", status=201)
        sink = HttpSink(url=httpserver.url_for(""), api_key="my-secret-key")
        await sink.write(_make_event())
        await sink.flush()
        await sink.aclose()
        httpserver.check_assertions()

    async def test_body_is_event_json(self, httpserver: HTTPServer) -> None:
        captured: list[dict[str, Any]] = []

        def handler(request: Any) -> Response:
            captured.append(request.get_json())
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        event = _make_event(sequence_number=42)
        await sink.write(event)
        await sink.flush()
        await sink.aclose()

        assert len(captured) == 1
        body = captured[0]
        assert body["event_type"] == "tool_call_start"
        assert body["session_id"] == "sess_test"
        assert body["sequence_number"] == 42
        assert body["payload"]["tool_name"] == "t"

    async def test_multiple_events_sent_in_order(self, httpserver: HTTPServer) -> None:
        seqs: list[int] = []

        def handler(request: Any) -> Response:
            seqs.append(request.get_json()["sequence_number"])
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        for i in range(5):
            await sink.write(_make_event(sequence_number=i + 1))
        await sink.flush()
        await sink.aclose()

        assert seqs == [1, 2, 3, 4, 5]


# =============================================================================
# Bounded buffer + overflow
# =============================================================================


class TestBufferOverflow:
    async def test_overflow_drops_oldest_with_userwarning(self, httpserver: HTTPServer) -> None:
        """Per SPEC §11.2 + CHARTER ADR-4: buffer is bounded; on overflow,
        oldest events are dropped and a UserWarning is emitted."""
        # Configure ingest to hang so events accumulate in the buffer.
        # We won't flush during the test; we just check the buffer state.
        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            buffer_size=3,
        )

        # Pre-fill the buffer beyond capacity; events go in but don't get sent
        # because we don't await flush.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            # Directly populate the buffer to test overflow semantics without
            # racing with the background flush task.
            for i in range(5):
                sink._enqueue_for_test(_make_event(sequence_number=i + 1))

        # 2 events dropped (capacity=3, pushed 5)
        assert len(sink._buffer) == 3
        # Oldest dropped: sequence_numbers in buffer are now 3, 4, 5
        seqs_in_buffer = [ev.sequence_number for ev in sink._buffer]
        assert seqs_in_buffer == [3, 4, 5]
        # UserWarning emitted
        warning_messages = [str(w.message) for w in caught if w.category is UserWarning]
        assert any("buffer" in msg.lower() and "drop" in msg.lower() for msg in warning_messages)

        await sink.aclose()

    async def test_userwarning_emitted_only_once(self, httpserver: HTTPServer) -> None:
        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            buffer_size=2,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for i in range(10):
                sink._enqueue_for_test(_make_event(sequence_number=i + 1))

        warning_count = sum(
            1 for w in caught if w.category is UserWarning and "drop" in str(w.message).lower()
        )
        assert warning_count == 1, f"expected exactly one warning, got {warning_count}"
        await sink.aclose()


# =============================================================================
# Auto-flush on emit (background drain task)
# =============================================================================


class TestAutoFlush:
    """emit() spawns a background drain task; events ship without an explicit
    flush() call. This is the standard telemetry-SDK pattern (Sentry, OTel,
    PostHog, ddtrace all do a variant of this)."""

    async def test_emit_alone_eventually_drains(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        await sink.write(_make_event())

        # No explicit flush() call. Wait for the background drain task to finish.
        assert sink._drain_task is not None
        await sink._drain_task

        assert len(sink._buffer) == 0
        httpserver.check_assertions()
        await sink.aclose()

    async def test_rapid_emits_share_single_drain_task(self, httpserver: HTTPServer) -> None:
        """Multiple rapid emits don't spawn redundant drain tasks. The
        in-flight task continues to see new events as they're enqueued."""
        seen: list[int] = []

        def handler(request: Any) -> Response:
            seen.append(request.get_json()["sequence_number"])
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)

        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        # Emit 5 events rapidly; the drain task should pick them all up
        for i in range(5):
            await sink.write(_make_event(sequence_number=i + 1))

        # Wait for whichever drain task is current to finish
        assert sink._drain_task is not None
        await sink._drain_task
        # Some events may have arrived during the first task's exit window;
        # call flush() to drain any stragglers.
        await sink.flush()

        assert sorted(seen) == [1, 2, 3, 4, 5]
        await sink.aclose()

    async def test_aclose_drains_pending_events(self, httpserver: HTTPServer) -> None:
        """aclose() waits for the background drain to complete, including any
        events emitted right before close."""
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        await sink.write(_make_event())
        # No flush — just close
        await sink.aclose()
        # Should have drained on close
        assert len(sink._buffer) == 0
        httpserver.check_assertions()


# =============================================================================
# Retry on transient failure
# =============================================================================


class TestRetry:
    async def test_retries_on_500_then_succeeds(self, httpserver: HTTPServer) -> None:
        # Use oneshot to queue specific responses
        httpserver.expect_oneshot_request("/v0/events", method="POST").respond_with_data(
            "", status=500
        )
        httpserver.expect_oneshot_request("/v0/events", method="POST").respond_with_data(
            "", status=500
        )
        httpserver.expect_oneshot_request("/v0/events", method="POST").respond_with_data(
            "", status=201
        )

        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            max_retries=3,
            backoff_base_seconds=0.001,
        )
        await sink.write(_make_event())
        await sink.flush()
        await sink.aclose()

        # Buffer should be empty — event eventually sent
        assert len(sink._buffer) == 0
        httpserver.check_assertions()

    async def test_does_not_retry_4xx(self, httpserver: HTTPServer) -> None:
        """4xx (except 429) are permanent; don't retry."""
        call_count = 0

        def handler(_request: Any) -> Response:
            nonlocal call_count
            call_count += 1
            return Response("bad request", status=400)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)

        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            max_retries=3,
            backoff_base_seconds=0.001,
        )
        await sink.write(_make_event())
        await sink.flush()
        await sink.aclose()

        # 4xx is permanent: one attempt, no retry. Event is dropped from buffer.
        assert call_count == 1
        assert len(sink._buffer) == 0


# =============================================================================
# Circuit breaker
# =============================================================================


class TestCircuitBreaker:
    async def test_opens_after_threshold_consecutive_failures(self, httpserver: HTTPServer) -> None:
        """After N consecutive transient failures, the circuit opens and
        subsequent emit attempts skip the HTTP path entirely (buffer events
        stay enqueued until the circuit allows requests again)."""
        call_count = 0

        def always_500(_request: Any) -> Response:
            nonlocal call_count
            call_count += 1
            return Response("err", status=500)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(always_500)

        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            max_retries=1,
            backoff_base_seconds=0.001,
            circuit_breaker_threshold=3,
            circuit_breaker_reset_seconds=60.0,
        )

        # Send 5 events; only the first 3 should trigger HTTP attempts
        # (max_retries=1 means 2 attempts each before counting as a failure).
        for _ in range(5):
            await sink.write(_make_event())
            await sink.flush()

        # 3 consecutive failed flushes x 2 attempts each = 6 HTTP calls
        # then circuit opens; remaining 2 emits skip HTTP entirely.
        assert call_count == 6
        # Events stay in buffer (circuit open; can't send)
        assert len(sink._buffer) == 5
        await sink.aclose()


# =============================================================================
# flush() + aclose()
# =============================================================================


class TestLifecycle:
    async def test_flush_drains_buffer(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        for i in range(10):
            await sink.write(_make_event(sequence_number=i + 1))
        await sink.flush()
        assert len(sink._buffer) == 0
        await sink.aclose()

    async def test_aclose_flushes_and_closes(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        await sink.write(_make_event())
        await sink.aclose()
        assert len(sink._buffer) == 0
        # Subsequent emit raises (closed)
        with pytest.raises(RuntimeError):
            await sink.write(_make_event(sequence_number=2))


# =============================================================================
# Shutdown-safe flush (atexit)
# =============================================================================


class TestShutdownFlush:
    """``_atexit_flush`` is the best-effort synchronous drain that runs when
    the process exits without an explicit ``aclose()``. Called directly here
    (bypassing real interpreter shutdown, same test-hook pattern as
    ``_enqueue_for_test``) since ``atexit`` itself only fires once per
    process and can't be exercised per-test."""

    async def test_registers_on_first_write_unregisters_on_close(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        assert sink._atexit_registered is False
        await sink.write(_make_event())
        assert sink._atexit_registered is True
        await sink.aclose()
        assert sink._atexit_registered is False

    async def test_flushes_leftover_buffer(self, httpserver: HTTPServer) -> None:
        """Events that never got drained (e.g. the background task was
        cancelled by an exiting event loop) are still in ``self._buffer`` —
        the atexit flush ships them via a fresh sync client."""
        posted: list[int] = []

        def handler(request: Any) -> Response:
            posted.append(request.get_json()["sequence_number"])
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        # Bypass the background drain — simulates a task that got cancelled
        # before it could send anything.
        sink._enqueue_for_test(_make_event(sequence_number=1))
        sink._enqueue_for_test(_make_event(sequence_number=2))

        sink._atexit_flush()

        assert posted == [1, 2]
        assert len(sink._buffer) == 0
        sink._closed = True  # avoid a second real flush attempt in aclose()

    async def test_noop_when_already_closed(self, httpserver: HTTPServer) -> None:
        call_count = 0

        def handler(_request: Any) -> Response:
            nonlocal call_count
            call_count += 1
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._enqueue_for_test(_make_event())
        sink._closed = True

        sink._atexit_flush()

        assert call_count == 0
        assert len(sink._buffer) == 1  # untouched

    async def test_noop_when_buffer_empty(self, httpserver: HTTPServer) -> None:
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._atexit_flush()  # must not raise with nothing buffered
        await sink.aclose()

    async def test_permanent_failure_dropped_without_retry(self, httpserver: HTTPServer) -> None:
        call_count = 0

        def handler(_request: Any) -> Response:
            nonlocal call_count
            call_count += 1
            return Response("bad request", status=400)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._enqueue_for_test(_make_event())

        sink._atexit_flush()

        assert call_count == 1
        assert len(sink._buffer) == 0
        sink._closed = True

    async def test_transient_failure_stops_and_leaves_buffer(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("err", status=500)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._enqueue_for_test(_make_event(sequence_number=1))
        sink._enqueue_for_test(_make_event(sequence_number=2))

        sink._atexit_flush()

        # Stops at the first failure — doesn't burn the shutdown deadline
        # retrying, and doesn't skip ahead to the next event.
        assert len(sink._buffer) == 2
        sink._closed = True

    async def test_skips_entirely_when_circuit_open(self, httpserver: HTTPServer) -> None:
        call_count = 0

        def handler(_request: Any) -> Response:
            nonlocal call_count
            call_count += 1
            return Response("err", status=500)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", circuit_breaker_threshold=3)
        for _ in range(3):
            sink._circuit.record_failure()
        assert sink._circuit.can_request() is False
        sink._enqueue_for_test(_make_event())

        sink._atexit_flush()

        assert call_count == 0
        assert len(sink._buffer) == 1
        sink._closed = True

    async def test_deadline_bounds_total_wall_time_across_events(
        self, httpserver: HTTPServer
    ) -> None:
        """The per-request timeout must be the REMAINING budget, not a fresh
        full timeout each time — otherwise N slow-but-successful events can
        blow the configured deadline by up to Nx."""
        import time

        def handler(_request: Any) -> Response:
            time.sleep(0.05)
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
        sink = HttpSink(
            url=httpserver.url_for(""), api_key="k", shutdown_flush_timeout_seconds=0.08
        )
        for i in range(5):
            sink._enqueue_for_test(_make_event(sequence_number=i + 1))

        start = time.monotonic()
        sink._atexit_flush()
        elapsed = time.monotonic() - start

        # 5 requests x 0.05s each would be ~0.25s if each got its own fresh
        # 0.08s budget (worse, unbounded); properly clamped it should stop
        # within roughly one deadline window of the 0.08s configured budget.
        assert elapsed < 0.2
        sink._closed = True


class TestCrossThreadShutdownFlush:
    """When a live event loop still owns the sink (the sync ``Client``'s
    background-thread bridge, still running because the vendor never called
    ``close()``), ``_atexit_flush`` must delegate to that loop via
    ``run_coroutine_threadsafe`` instead of touching ``self._buffer`` /
    ``self._http_client`` directly from the calling (atexit/main) thread."""

    async def test_delegates_to_live_loop_instead_of_racing_it(
        self, httpserver: HTTPServer
    ) -> None:
        import threading
        import time

        posted: list[int] = []
        lock = threading.Lock()

        def handler(request: Any) -> Response:
            seq = request.get_json()["sequence_number"]
            if seq == 1:
                # Widen the window so the background drain is still mid-flight
                # on event 1 when _atexit_flush runs from this thread.
                time.sleep(0.1)
            with lock:
                posted.append(seq)
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)

        # A second thread running its own persistent loop — the same shape
        # as baton.client._SyncBridge.
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        ready.wait(timeout=5.0)

        sink = HttpSink(url=httpserver.url_for(""), api_key="k")

        async def write_both() -> None:
            await sink.write(_make_event(sequence_number=1))
            await sink.write(_make_event(sequence_number=2))

        try:
            asyncio.run_coroutine_threadsafe(write_both(), loop).result(timeout=5.0)
            # Let the bridge thread's background drain actually start
            # sending event 1 before calling _atexit_flush from THIS
            # (the test/main) thread — a genuine cross-thread call.
            time.sleep(0.02)
            sink._atexit_flush()

            # Delegating to the live loop (which awaits the in-flight drain
            # task, then runs flush() under its own lock) means every event
            # is sent exactly once — no duplicate POST from a second sender
            # racing the buffer, no drop from a pop lost to the race.
            assert sorted(posted) == [1, 2]
            assert len(posted) == 2
            assert len(sink._buffer) == 0
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)
            loop.close()


class TestAtexitDoesNotPinUnclosedSink:
    async def test_unclosed_sink_is_still_garbage_collectable(self, httpserver: HTTPServer) -> None:
        """A sink that's written to but never explicitly closed must not be
        kept alive for the interpreter's lifetime just by being registered
        with atexit — the registration is weakref-based specifically so a
        caller who drops all other references can still reclaim it."""
        import gc
        import weakref

        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        await sink.write(_make_event())
        assert sink._drain_task is not None
        await sink._drain_task  # let it actually drain before dropping the reference

        ref = weakref.ref(sink)
        del sink
        gc.collect()

        assert ref() is None


# =============================================================================
# StdoutSink
# =============================================================================


class TestStdoutSink:
    async def test_writes_jsonl_to_stream(self) -> None:
        import io

        from baton.sinks import StdoutSink

        buf = io.StringIO()
        sink = StdoutSink(stream=buf)
        await sink.write(_make_event(sequence_number=1))
        await sink.write(_make_event(sequence_number=2))
        await sink.aclose()

        lines = buf.getvalue().splitlines()
        assert len(lines) == 2
        # Each line is parseable JSON with the expected envelope
        import json

        first = json.loads(lines[0])
        assert first["event_type"] == "tool_call_start"
        assert first["sequence_number"] == 1

    async def test_defaults_to_stderr(self) -> None:
        """StdoutSink defaults to stderr (NOT stdout) — the MCP stdio
        transport reserves stdout for JSON-RPC; writing capture events
        there would corrupt the protocol stream."""
        import sys

        from baton.sinks import StdoutSink

        sink = StdoutSink()
        try:
            assert sink._stream is sys.stderr
        finally:
            await sink.aclose()

    async def test_write_after_close_raises(self) -> None:
        import io

        from baton.sinks import StdoutSink

        sink = StdoutSink(stream=io.StringIO())
        await sink.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await sink.write(_make_event())

    async def test_does_not_close_user_owned_stream(self) -> None:
        """aclose() should not close the underlying stream — the caller
        owns stderr / stdout / their own buffer."""
        import io

        from baton.sinks import StdoutSink

        buf = io.StringIO()
        sink = StdoutSink(stream=buf)
        await sink.aclose()
        assert not buf.closed


# =============================================================================
# FileSink
# =============================================================================


class TestFileSink:
    async def test_appends_jsonl_to_file(self, tmp_path: Any) -> None:
        from baton.sinks import FileSink

        path = tmp_path / "events.jsonl"
        sink = FileSink(path)
        await sink.write(_make_event(sequence_number=1))
        await sink.write(_make_event(sequence_number=2))
        await sink.aclose()

        lines = path.read_text().splitlines()
        assert len(lines) == 2
        import json

        assert json.loads(lines[0])["sequence_number"] == 1
        assert json.loads(lines[1])["sequence_number"] == 2

    async def test_lazy_file_creation(self, tmp_path: Any) -> None:
        """Constructing a FileSink without writing to it should not create
        an empty file."""
        from baton.sinks import FileSink

        path = tmp_path / "untouched.jsonl"
        sink = FileSink(path)
        await sink.aclose()
        assert not path.exists()

    async def test_appends_across_two_sinks(self, tmp_path: Any) -> None:
        """A second FileSink pointed at the same path appends, not overwrites
        — important for restart-safe capture."""
        from baton.sinks import FileSink

        path = tmp_path / "events.jsonl"
        sink_a = FileSink(path)
        await sink_a.write(_make_event(sequence_number=1))
        await sink_a.aclose()

        sink_b = FileSink(path)
        await sink_b.write(_make_event(sequence_number=2))
        await sink_b.aclose()

        lines = path.read_text().splitlines()
        assert len(lines) == 2

    async def test_write_after_close_raises(self, tmp_path: Any) -> None:
        from baton.sinks import FileSink

        sink = FileSink(tmp_path / "x.jsonl")
        await sink.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await sink.write(_make_event())


# =============================================================================
# MultiSink
# =============================================================================


class TestMultiSink:
    async def test_fans_out_to_all_sinks(self, tmp_path: Any) -> None:
        import io

        from baton.sinks import FileSink, MultiSink, StdoutSink

        buf = io.StringIO()
        path = tmp_path / "events.jsonl"
        sink = MultiSink([StdoutSink(stream=buf), FileSink(path)])

        await sink.write(_make_event(sequence_number=1))
        await sink.aclose()

        # Both sinks received the event
        assert "tool_call_start" in buf.getvalue()
        assert "tool_call_start" in path.read_text()

    async def test_empty_list_rejected(self) -> None:
        from baton.sinks import MultiSink

        with pytest.raises(ValueError, match="at least one"):
            MultiSink([])

    async def test_one_failure_does_not_block_others(self, tmp_path: Any) -> None:
        """A failure in one sink must not prevent the others from being
        written to. The exception is re-raised after fan-out completes."""
        import io

        from baton.sinks import FileSink, MultiSink, Sink

        class _AlwaysFailSink(Sink):
            async def write(self, event: Any) -> None:
                raise RuntimeError("broken sink")

            async def flush(self) -> None:
                pass

            async def aclose(self) -> None:
                pass

        good_buf = io.StringIO()

        class _BufferSink(Sink):
            async def write(self, event: Any) -> None:
                good_buf.write("written\n")

            async def flush(self) -> None:
                pass

            async def aclose(self) -> None:
                pass

        path = tmp_path / "events.jsonl"
        sink = MultiSink([_AlwaysFailSink(), _BufferSink(), FileSink(path)])

        with pytest.raises(RuntimeError, match="broken sink"):
            await sink.write(_make_event())

        # The two healthy sinks still got the event
        assert good_buf.getvalue() == "written\n"
        assert "tool_call_start" in path.read_text()

        await sink.aclose()

    async def test_multiple_failures_raise_exception_group(self, tmp_path: Any) -> None:
        from baton.sinks import MultiSink, Sink

        class _AlwaysFailSink(Sink):
            def __init__(self, msg: str) -> None:
                self._msg = msg

            async def write(self, event: Any) -> None:
                raise RuntimeError(self._msg)

            async def flush(self) -> None:
                pass

            async def aclose(self) -> None:
                pass

        sink = MultiSink([_AlwaysFailSink("first"), _AlwaysFailSink("second")])

        with pytest.raises(ExceptionGroup) as info:
            await sink.write(_make_event())
        msgs = {str(e) for e in info.value.exceptions}
        assert msgs == {"first", "second"}
