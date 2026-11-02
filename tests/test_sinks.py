"""Tests for the sinks; ``HttpSink`` per SPEC §2.5 and §11.2, against
pytest-httpserver's real in-process HTTP server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import pytest
import urllib3
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

import baton.sinks as sinks_module
from baton._state import PROCESS_DROPS
from baton.events import ToolCallStartEvent
from baton.sinks import BATCH_MAX_BYTES, HttpSink, _BatchSender
from tests._http_fakes import Answer, fake_sink, make_event, never_answers, until

BATCH = "/v0/events/batch"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _seqs(request: Any) -> list[int]:
    """The sequence numbers of a batch's events, leaving out a ``client_report``."""
    events = request.get_json()["events"]
    return [e["sequence_number"] for e in events if e["event_type"] != "client_report"]


# =============================================================================
# Basic POST to the batch endpoint
# =============================================================================


class TestEmitPostsToIngest:
    async def test_events_are_posted_to_v0_events_batch(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="test-key")
        await sink.write(make_event())
        await sink.flush()
        await sink.aclose()
        httpserver.check_assertions()
        assert len(httpserver.log) == 1

    async def test_bearer_auth_header(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(
            BATCH,
            method="POST",
            headers={"Authorization": "Bearer my-secret-key"},
        ).respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="my-secret-key")
        await sink.write(make_event())
        await sink.flush()
        await sink.aclose()
        httpserver.check_assertions()

    async def test_body_is_the_events_json(self, httpserver: HTTPServer) -> None:
        captured: list[dict[str, Any]] = []

        def handler(request: Any) -> Response:
            captured.append(request.get_json())
            return Response("", status=200)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        event = make_event(sequence_number=42)
        await sink.write(event)
        await sink.flush()
        await sink.aclose()

        assert len(captured) == 1
        [body] = captured[0]["events"]
        assert body["event_type"] == "tool_call_start"
        assert body["session_id"] == "sess_test"
        assert body["sequence_number"] == 42
        assert body["payload"]["tool_name"] == "t"

    async def test_multiple_events_sent_in_order_in_one_batch(self, httpserver: HTTPServer) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        for i in range(5):
            await sink.write(make_event(sequence_number=i + 1))
        await sink.flush()
        await sink.aclose()

        assert batches == [[1, 2, 3, 4, 5]]


# =============================================================================
# Bounded buffer + overflow
# =============================================================================


class TestBufferOverflow:
    async def test_overflow_drops_oldest_with_userwarning(self, httpserver: HTTPServer) -> None:
        """Per SPEC §11.2 + CHARTER ADR-4: buffer is bounded; on overflow,
        oldest events are dropped and a UserWarning is emitted."""
        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            buffer_size=3,
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            # Queued without starting the sending thread, so nothing is sent.
            for i in range(5):
                sink._sender.enqueue(make_event(sequence_number=i + 1))

        assert sink._sender.pending() == 3
        seqs_in_buffer = [p.event.sequence_number for p in sink._sender.waiting]
        assert seqs_in_buffer == [3, 4, 5]
        warning_messages = [str(w.message) for w in caught if w.category is UserWarning]
        assert any("buffer" in msg.lower() and "drop" in msg.lower() for msg in warning_messages)
        assert PROCESS_DROPS.total() == {"buffer_full": 2}
        sink._closed = True

    async def test_userwarning_emitted_only_once(self, httpserver: HTTPServer) -> None:
        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            buffer_size=2,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for i in range(10):
                sink._sender.enqueue(make_event(sequence_number=i + 1))

        warning_count = sum(
            1 for w in caught if w.category is UserWarning and "drop" in str(w.message).lower()
        )
        assert warning_count == 1, f"expected exactly one warning, got {warning_count}"
        sink._closed = True


# =============================================================================
# When a batch goes (SPEC §11.2 rule 4)
# =============================================================================


def _recording_batches(httpserver: HTTPServer) -> list[list[int]]:
    batches: list[list[int]] = []

    def handler(request: Any) -> Response:
        if seqs := _seqs(request):
            batches.append(seqs)
        return Response("", status=200)

    httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
    return batches


class TestWhenABatchGoes:
    async def test_the_timer_sends_after_the_interval_and_not_before(
        self, httpserver: HTTPServer
    ) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=0.3)
        written = time.monotonic()
        for n in (1, 2, 3):
            await sink.write(make_event(sequence_number=n))
        await asyncio.sleep(0.15)
        assert batches == []
        await until(lambda: batches)
        assert time.monotonic() - written >= 0.3
        assert batches == [[1, 2, 3]]
        await sink.aclose()

    async def test_flush_sends_at_once(self, httpserver: HTTPServer) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=60)
        await sink.write(make_event(sequence_number=1))
        started = time.monotonic()
        await sink.flush()
        assert time.monotonic() - started < 1.0
        assert batches == [[1]]
        await sink.aclose()

    async def test_100_waiting_events_go_without_waiting_for_the_timer(
        self, httpserver: HTTPServer
    ) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=60)
        for n in range(1, 151):
            await sink.write(make_event(sequence_number=n))
        await until(lambda: batches)
        assert batches == [list(range(1, 101))]
        await sink.flush()
        assert batches[1:] == [list(range(101, 151))]
        await sink.aclose()

    async def test_the_byte_cap_cuts_a_batch_and_sends_it_without_the_timer(
        self, httpserver: HTTPServer
    ) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=60)
        events = [
            make_event(sequence_number=1),
            _make_event_with_params(2, {"blob": "x" * 600_000}),
            _make_event_with_params(3, {"blob": "x" * 600_000}),
            make_event(sequence_number=4),
        ]
        for event in events:
            await sink.write(event)
        await until(lambda: len(batches) >= 1)
        await asyncio.sleep(0.1)
        assert batches == [[1, 2]], "the rest waits for the timer"
        await sink.flush()
        assert batches == [[1, 2], [3, 4]]
        assert all(len(r.data) <= BATCH_MAX_BYTES for r, _ in httpserver.log)
        await sink.aclose()

    async def test_an_event_alone_over_the_cap_is_never_sent_and_is_refused(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=60)
        with caplog.at_level(logging.WARNING, logger="baton"):
            await sink.write(_make_event_with_params(1, {"blob": "x" * BATCH_MAX_BYTES}))
            await sink.write(make_event(sequence_number=2))
            await sink.flush()
        assert batches == [[2]]
        assert PROCESS_DROPS.total() == {"refused": 1}
        assert any("over the 1 MiB batch cap" in m for m in caplog.messages)
        await sink.aclose()

    async def test_a_413_on_one_event_refuses_it(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=413)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        await sink.write(make_event())
        await sink.flush()
        assert len(httpserver.log) == 1
        assert PROCESS_DROPS.total() == {"refused": 1}
        await sink.aclose()

    async def test_aclose_drains_pending_events(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        await sink.write(make_event())
        await sink.aclose()
        assert sink._sender.pending() == 0
        httpserver.check_assertions()

    async def test_one_request_at_a_time(self, httpserver: HTTPServer) -> None:
        in_flight = 0
        most = 0
        lock = threading.Lock()

        def handler(request: Any) -> Response:
            nonlocal in_flight, most
            with lock:
                in_flight += 1
                most = max(most, in_flight)
            time.sleep(0.02)
            with lock:
                in_flight -= 1
            return Response("", status=200)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=0)
        for n in range(1, 401):
            await sink.write(make_event(sequence_number=n))
            if n % 50 == 0:
                await asyncio.sleep(0.005)
        await sink.aclose()
        assert len(httpserver.log) >= 4
        assert most == 1


# =============================================================================
# What each answer does (SPEC §2.5)
# =============================================================================


class TestAnswers:
    async def test_413_splits_the_batch_in_two_until_it_fits(self, httpserver: HTTPServer) -> None:
        sizes: list[int] = []

        def handler(request: Any) -> Response:
            events = request.get_json()["events"]
            sizes.append(len(events))
            return Response("", status=413 if len(events) > 2 else 200)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        for n in range(1, 6):
            await sink.write(make_event(sequence_number=n))
        await sink.flush()
        assert sizes == [5, 2, 3, 1, 2]
        assert PROCESS_DROPS.total() == {}
        assert sink._sender.pending() == 0
        await sink.aclose()

    @pytest.mark.parametrize("status", [404, 405])
    async def test_a_collector_without_the_batch_endpoint_gets_one_event_per_request(
        self, httpserver: HTTPServer, status: int
    ) -> None:
        single: list[int] = []

        def one(request: Any) -> Response:
            single.append(request.get_json()["sequence_number"])
            return Response("", status=202)

        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=status)
        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(one)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        for n in (1, 2, 3):
            await sink.write(make_event(sequence_number=n))
        await sink.flush()
        await sink.write(make_event(sequence_number=4))
        await sink.flush()
        await sink.aclose()
        assert single == [1, 2, 3, 4]
        assert [r.path for r, _ in httpserver.log].count(BATCH) == 1
        assert PROCESS_DROPS.total() == {}

    async def test_a_refused_result_is_counted_and_the_rest_are_delivered(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        def handler(request: Any) -> Response:
            events = request.get_json()["events"]
            results = [
                {"event_id": e["event_id"], "status": s}
                | (
                    {"error": {"code": "payload_malformed", "message": "no tool_name"}}
                    if s == "refused"
                    else {}
                )
                for e, s in zip(events, ["accepted", "refused", "duplicate"], strict=True)
            ]
            return Response(json.dumps({"results": results}), status=200)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        with caplog.at_level(logging.WARNING, logger="baton"):
            for n in (1, 2, 3):
                await sink.write(make_event(sequence_number=n))
            await sink.flush()
        assert PROCESS_DROPS.total() == {"refused": 1}
        assert sink._sender.pending() == 0
        [warning] = [r.getMessage() for r in caplog.records if "REFUSED" in r.getMessage()]
        assert "payload_malformed: no tool_name" in warning
        await sink.aclose()

    async def test_results_are_matched_by_event_id_not_position(
        self, httpserver: HTTPServer
    ) -> None:
        def handler(request: Any) -> Response:
            ids = [e["event_id"] for e in request.get_json()["events"]]
            results = [
                {"event_id": "not-in-this-batch", "status": "refused"},
                {"event_id": ids[2], "status": "accepted"},
                {"event_id": ids[1], "status": "refused"},
                {"event_id": ids[0], "status": "accepted"},
            ]
            return Response(json.dumps({"results": results}), status=200)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        for n in (1, 2, 3):
            await sink.write(make_event(sequence_number=n))
        await sink.flush()
        assert PROCESS_DROPS.total() == {"refused": 1}
        await sink.aclose()

    async def test_429_keeps_the_events_and_waits_retry_after(self, httpserver: HTTPServer) -> None:
        times: list[float] = []

        def handler(request: Any) -> Response:
            times.append(time.monotonic())
            if len(times) == 1:
                return Response("", status=429, headers={"Retry-After": "1"})
            return Response("", status=200)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=0.05)
        await sink.write(make_event())
        await sink.flush()
        assert len(times) == 1 and sink._sender.pending() == 1
        await sink.flush()
        assert len(times) == 1, "a flush does not send before Retry-After"
        await until(lambda: not sink._sender.pending())
        assert times[1] - times[0] >= 1.0
        assert PROCESS_DROPS.total() == {}
        await sink.aclose()

    @pytest.mark.parametrize("status", [400, 401, 403, 422])
    async def test_another_4xx_refuses_the_whole_batch(
        self, httpserver: HTTPServer, status: int
    ) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("no", status=status)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        for n in (1, 2, 3):
            await sink.write(make_event(sequence_number=n))
        await sink.flush()
        assert PROCESS_DROPS.total() == {"refused": 3}
        assert len(httpserver.log) == 1
        await sink.aclose()

    async def test_a_failed_batch_is_retried_when_the_timer_fires(
        self, httpserver: HTTPServer
    ) -> None:
        answers = iter([500, 200])
        httpserver.expect_request(BATCH, method="POST").respond_with_handler(
            lambda request: Response("", status=next(answers))
        )
        sink = HttpSink(
            url=httpserver.url_for(""), api_key="k", max_retries=0, flush_interval_seconds=0.1
        )
        await sink.write(make_event())
        await until(lambda: not sink._sender.pending())
        assert len(httpserver.log) == 2
        await sink.aclose()

    async def test_a_batch_holds_one_tenant(self, httpserver: HTTPServer) -> None:
        tenants: list[set[str]] = []

        def handler(request: Any) -> Response:
            tenants.append({e["tenant_id"] for e in request.get_json()["events"]})
            return Response("", status=200)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        other = make_event(sequence_number=2)
        other.tenant_id = "ten_other"
        for event in (make_event(sequence_number=1), other, make_event(sequence_number=3)):
            await sink.write(event)
        await sink.flush()
        assert tenants == [{"ten_test"}, {"ten_other"}, {"ten_test"}]
        await sink.aclose()


class TestNoLogLinePerRequest:
    async def test_sending_at_info_logs_nothing(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        with caplog.at_level(logging.INFO):
            for n in range(1, 4):
                await sink.write(make_event(sequence_number=n))
                await sink.flush()
            await sink.aclose()
        assert len(httpserver.log) == 3
        ours = [r for r in caplog.records if r.name.split(".")[0] in ("baton", "urllib3")]
        assert [(r.name, r.getMessage()) for r in ours] == []


# =============================================================================
# Retry on transient failure
# =============================================================================


class TestRetry:
    async def test_retries_on_500_then_succeeds(self, httpserver: HTTPServer) -> None:
        for status in (500, 500, 200):
            httpserver.expect_oneshot_request(BATCH, method="POST").respond_with_data(
                "", status=status
            )

        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            max_retries=3,
            backoff_base_seconds=0.001,
        )
        await sink.write(make_event())
        await sink.flush()
        await sink.aclose()

        assert sink._sender.pending() == 0
        httpserver.check_assertions()

    async def test_does_not_retry_4xx(self, httpserver: HTTPServer) -> None:
        """4xx (except 429) are permanent; don't retry."""
        call_count = 0

        def handler(_request: Any) -> Response:
            nonlocal call_count
            call_count += 1
            return Response("bad request", status=400)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)

        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            max_retries=3,
            backoff_base_seconds=0.001,
        )
        await sink.write(make_event())
        await sink.flush()
        await sink.aclose()

        assert call_count == 1
        assert sink._sender.pending() == 0


# =============================================================================
# Circuit breaker
# =============================================================================


class TestCircuitBreaker:
    async def test_opens_after_threshold_consecutive_failures(self, httpserver: HTTPServer) -> None:
        """After N consecutive failed sends the circuit opens and later
        flushes send nothing; the events stay queued."""
        call_count = 0

        def always_500(_request: Any) -> Response:
            nonlocal call_count
            call_count += 1
            return Response("err", status=500)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(always_500)

        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            max_retries=1,
            backoff_base_seconds=0.001,
            circuit_breaker_threshold=3,
            circuit_breaker_reset_seconds=60.0,
        )

        for _ in range(5):
            await sink.write(make_event())
            await sink.flush()

        # 3 failed flushes x 2 attempts each; then the circuit is open.
        assert call_count == 6
        assert sink._sender.pending() == 5
        await sink.aclose()


# =============================================================================
# flush() + aclose()
# =============================================================================


class TestLifecycle:
    async def test_flush_drains_buffer(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        for i in range(10):
            await sink.write(make_event(sequence_number=i + 1))
        await sink.flush()
        assert sink._sender.pending() == 0
        await sink.aclose()

    async def test_aclose_flushes_and_closes(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        await sink.write(make_event())
        await sink.aclose()
        assert sink._sender.pending() == 0
        with pytest.raises(RuntimeError):
            await sink.write(make_event(sequence_number=2))

    async def test_aclose_keeps_its_budget_against_a_collector_that_never_answers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            sink = HttpSink(
                url=f"http://127.0.0.1:{port}",
                api_key="k",
                request_timeout_seconds=30,
                shutdown_flush_timeout_seconds=0.3,
            )
            await sink.write(make_event())
            started = time.monotonic()
            with caplog.at_level(logging.WARNING, logger="baton"):
                await sink.aclose()
            assert time.monotonic() - started < 0.6
        assert PROCESS_DROPS.total() == {"unsent_at_close": 1}
        assert "baton: the sink closed with 1 event(s) unsent." in caplog.messages

    async def test_write_does_not_wait_on_the_network(self) -> None:
        sink = fake_sink(never_answers, flush_interval_seconds=0, shutdown_flush_timeout_seconds=0)
        started = time.monotonic()
        for n in range(1, 201):
            await sink.write(make_event(sequence_number=n))
        assert time.monotonic() - started < 0.5
        await sink.aclose()

    async def test_the_sending_thread_is_a_daemon(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        await sink.write(make_event())
        thread = sink._sender.thread
        assert thread is not None and thread.daemon
        await sink.aclose()
        thread.join(timeout=2)
        assert not thread.is_alive()


# =============================================================================
# Shutdown-safe flush (atexit)
# =============================================================================


class TestShutdownFlush:
    """``_atexit_flush`` runs when the process exits without ``aclose()``.
    Called directly here, since ``atexit`` fires once per process; the
    subprocess test at the end runs the real one."""

    async def test_registers_on_first_write_unregisters_on_close(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        assert sink._atexit_callback is None
        await sink.write(make_event())
        assert sink._atexit_callback is not None
        await sink.aclose()
        assert sink._atexit_callback is None

    async def test_flushes_leftover_buffer_with_no_sending_thread(
        self, httpserver: HTTPServer
    ) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._sender.enqueue(make_event(sequence_number=1))
        sink._sender.enqueue(make_event(sequence_number=2))

        sink._atexit_flush()

        assert sink._sender.thread is None
        assert batches == [[1, 2]]
        assert sink._sender.pending() == 0
        sink._closed = True

    async def test_hands_the_flush_to_a_live_sending_thread(self, httpserver: HTTPServer) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=60)
        await sink.write(make_event(sequence_number=1))
        assert sink._sender.thread_alive()
        sink._atexit_flush()
        assert batches == [[1]]
        sink._closed = True

    async def test_noop_when_already_closed(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._sender.enqueue(make_event())
        sink._closed = True

        sink._atexit_flush()

        assert len(httpserver.log) == 0
        assert sink._sender.pending() == 1

    async def test_noop_when_buffer_empty(self, httpserver: HTTPServer) -> None:
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._atexit_flush()
        await sink.aclose()

    async def test_permanent_failure_dropped_without_retry(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("bad", status=400)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._sender.enqueue(make_event())

        sink._atexit_flush()

        assert len(httpserver.log) == 1
        assert sink._sender.pending() == 0
        sink._closed = True

    async def test_transient_failure_stops_and_leaves_buffer(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("err", status=500)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", backoff_base_seconds=0.001)
        sink._sender.enqueue(make_event(sequence_number=1))
        sink._sender.enqueue(make_event(sequence_number=2))

        sink._atexit_flush()

        assert sink._sender.pending() == 2
        sink._closed = True

    async def test_skips_entirely_when_circuit_open(self, httpserver: HTTPServer) -> None:
        sink = HttpSink(url=httpserver.url_for(""), api_key="k", circuit_breaker_threshold=3)
        for _ in range(3):
            sink._circuit.record_failure()
        assert sink._circuit.can_request() is False
        sink._sender.enqueue(make_event())

        sink._atexit_flush()

        assert len(httpserver.log) == 0
        assert sink._sender.pending() == 1
        sink._closed = True

    @pytest.mark.parametrize("thread", [False, True])
    async def test_the_budget_bounds_the_exit_flush(
        self, httpserver: HTTPServer, thread: bool
    ) -> None:
        def handler(_request: Any) -> Response:
            time.sleep(0.5)
            return Response("", status=200)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            shutdown_flush_timeout_seconds=0.08,
            flush_interval_seconds=60,
        )
        for i in range(5):
            if thread:
                await sink.write(make_event(sequence_number=i + 1))
            else:
                sink._sender.enqueue(make_event(sequence_number=i + 1))

        start = time.monotonic()
        sink._atexit_flush()
        assert time.monotonic() - start < 0.3
        sink._closed = True


class TestCrossThreadShutdownFlush:
    """The sync ``Client`` writes from its bridge thread's loop while the exit
    flush runs on the main thread: every event still goes exactly once."""

    async def test_an_exit_flush_beside_a_live_loop_sends_each_event_once(
        self, httpserver: HTTPServer
    ) -> None:
        posted: list[int] = []
        lock = threading.Lock()

        def handler(request: Any) -> Response:
            seqs = _seqs(request)
            if 1 in seqs:
                time.sleep(0.1)
            with lock:
                posted.extend(seqs)
            return Response("", status=200)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)

        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        ready.wait(timeout=5.0)

        sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=0)

        async def write(n: int) -> None:
            await sink.write(make_event(sequence_number=n))

        try:
            asyncio.run_coroutine_threadsafe(write(1), loop).result(timeout=5.0)
            time.sleep(0.02)  # event 1 is being sent
            asyncio.run_coroutine_threadsafe(write(2), loop).result(timeout=5.0)
            sink._atexit_flush()

            assert sorted(posted) == [1, 2]
            assert len(posted) == 2
            assert sink._sender.pending() == 0
        finally:
            sink._closed = True
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)
            loop.close()


def test_a_process_that_exits_without_close_still_sends(httpserver: HTTPServer) -> None:
    batches = _recording_batches(httpserver)
    script = f"""
import asyncio
from tests._http_fakes import make_event
from baton.sinks import HttpSink

async def main():
    sink = HttpSink(url={httpserver.url_for("")!r}, api_key="k")
    await sink.write(make_event(sequence_number=7))

asyncio.run(main())
"""
    started = time.monotonic()
    subprocess.run([sys.executable, "-c", script], check=True, timeout=20, cwd=_REPO_ROOT)
    assert time.monotonic() - started < 10
    assert batches == [[7]]


class TestAtexitDoesNotPinUnclosedSink:
    async def test_unclosed_sink_is_still_garbage_collectable(self, httpserver: HTTPServer) -> None:
        """A sink written to but never closed must not be kept alive by its
        exit hook or its sending thread; once collected, its thread ends."""
        import gc
        import weakref

        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        await sink.write(make_event())
        await sink.flush()
        thread = sink._sender.thread
        assert thread is not None

        ref = weakref.ref(sink)
        del sink
        gc.collect()

        assert ref() is None
        thread.join(timeout=2)
        assert not thread.is_alive()


# =============================================================================
# StdoutSink
# =============================================================================


class TestStdoutSink:
    async def test_writes_jsonl_to_stream(self) -> None:
        import io

        from baton.sinks import StdoutSink

        buf = io.StringIO()
        sink = StdoutSink(stream=buf)
        await sink.write(make_event(sequence_number=1))
        await sink.write(make_event(sequence_number=2))
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
            await sink.write(make_event())

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
        await sink.write(make_event(sequence_number=1))
        await sink.write(make_event(sequence_number=2))
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
        await sink_a.write(make_event(sequence_number=1))
        await sink_a.aclose()

        sink_b = FileSink(path)
        await sink_b.write(make_event(sequence_number=2))
        await sink_b.aclose()

        lines = path.read_text().splitlines()
        assert len(lines) == 2

    async def test_write_after_close_raises(self, tmp_path: Any) -> None:
        from baton.sinks import FileSink

        sink = FileSink(tmp_path / "x.jsonl")
        await sink.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await sink.write(make_event())


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

        await sink.write(make_event(sequence_number=1))
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
            await sink.write(make_event())

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
            await sink.write(make_event())
        msgs = {str(e) for e in info.value.exceptions}
        assert msgs == {"first", "second"}


class TestARejectedEventSaysSo:
    """A refused event must not vanish silently: the collector's status and
    reason are logged. The circuit breaker is not told: a 4xx proves the
    endpoint is alive, and opening the circuit would stop the good events too.
    """

    async def test_the_status_and_body_are_logged_when_an_event_is_refused(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data(
            '{"detail":"envelope carries both `principal` and `principal_id`"}', status=422
        )
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._sender.enqueue(make_event())

        with caplog.at_level(logging.WARNING, logger="baton"):
            await sink.flush()

        assert sink._sender.pending() == 0, (
            "the event is still dropped — this is not a retry change"
        )
        blob = caplog.text
        assert "422" in blob, f"the status must name itself: {blob!r}"
        assert "principal" in blob, f"the collector's reason must survive: {blob!r}"
        assert sink._circuit._failures == 0
        sink._closed = True

    async def test_a_2xx_stays_quiet(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        httpserver.expect_request(BATCH, method="POST").respond_with_data("", status=200)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._sender.enqueue(make_event())

        with caplog.at_level(logging.WARNING, logger="baton"):
            await sink.flush()

        assert sink._sender.pending() == 0
        assert caplog.text == "", f"a successful send logged: {caplog.text!r}"
        sink._closed = True


class TestRepeatedRefusalsAreCounted:
    @staticmethod
    def _sink(httpserver: HTTPServer, answers: list[tuple[int, str]]) -> HttpSink:
        def handler(request: Any) -> Response:
            status, body = answers.pop(0)
            return Response(body, status=status)

        httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
        return HttpSink(url=httpserver.url_for(""), api_key="k")

    @staticmethod
    async def _send(sink: HttpSink, n: int) -> None:
        """One request per event."""
        for i in range(n):
            sink._sender.enqueue(make_event(i))
            await sink.flush()

    @staticmethod
    def _refusal_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
        return [r.getMessage() for r in caplog.records if "REFUSED" in r.getMessage()]

    async def test_a_repeated_status_warns_once_then_says_how_many_it_skipped(
        self,
        httpserver: HTTPServer,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sink = self._sink(httpserver, [(422, "bad")] * 4)
        with caplog.at_level(logging.WARNING, logger="baton"):
            await self._send(sink, 3)
            assert len(self._refusal_warnings(caplog)) == 1
            assert PROCESS_DROPS.total() == {"refused": 3}

            monkeypatch.setattr(_BatchSender, "REFUSAL_LOG_SECONDS", 0.0)
            await self._send(sink, 1)

        warnings = self._refusal_warnings(caplog)
        assert len(warnings) == 2
        assert warnings[1].endswith(" 2 more refused since the last such warning.")
        assert not warnings[0].endswith("warning.")
        sink._closed = True

    async def test_one_answer_refusing_a_batch_is_one_warning_naming_the_count(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = self._sink(httpserver, [(422, "bad")])
        for i in range(3):
            sink._sender.enqueue(make_event(i))
        with caplog.at_level(logging.WARNING, logger="baton"):
            await sink.flush()
        [warning] = self._refusal_warnings(caplog)
        assert "REFUSED 3 events" in warning
        assert PROCESS_DROPS.total() == {"refused": 3}
        sink._closed = True

    async def test_a_new_status_warns_at_once(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = self._sink(httpserver, [(422, "bad"), (401, "no key")])
        with caplog.at_level(logging.WARNING, logger="baton"):
            await self._send(sink, 2)
        warnings = self._refusal_warnings(caplog)
        assert len(warnings) == 2
        assert "(HTTP 422)" in warnings[0] and "(HTTP 401)" in warnings[1]
        sink._closed = True

    async def test_close_names_the_refusals_not_yet_warned(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = self._sink(httpserver, [(422, "bad"), (422, "bad"), (401, "no"), (401, "no")])
        with caplog.at_level(logging.WARNING, logger="baton"):
            await self._send(sink, 4)
            await sink.aclose()
        assert (
            "baton: the sink closed; 2 more event(s) refused since the last such warning."
            in caplog.messages
        )

    async def test_an_exit_without_close_names_them_too(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = self._sink(httpserver, [(422, "bad"), (422, "bad")])
        with caplog.at_level(logging.WARNING, logger="baton"):
            await self._send(sink, 2)
            sink._atexit_flush()
        assert (
            "baton: process exiting; 1 more event(s) refused since the last such warning."
            in caplog.messages
        )
        sink._closed = True

    def test_the_exit_count_includes_refusals_during_the_exit_flush(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = self._sink(httpserver, [(422, "bad")] * 2)
        with caplog.at_level(logging.WARNING, logger="baton"):
            asyncio.run(self._send(sink, 1))
            sink._sender.enqueue(make_event(1))
            sink._sender.enqueue(make_event(2))
            sink._atexit_flush()
            assert len(self._refusal_warnings(caplog)) == 1
            assert (
                "baton: process exiting; 2 more event(s) refused since the last such warning."
                in caplog.messages
            )
            asyncio.run(sink.aclose())
        assert not any("the sink closed;" in m for m in caplog.messages), (
            "a count already logged at exit is not logged again at close"
        )

    async def test_a_quiet_close_says_nothing_about_refusals(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = self._sink(httpserver, [(422, "bad")])
        with caplog.at_level(logging.WARNING, logger="baton"):
            await self._send(sink, 1)
            await sink.aclose()
        assert not any("the sink closed;" in m for m in caplog.messages)

    async def test_the_collectors_text_is_escaped_after_the_cap(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        cap = _BatchSender.REFUSAL_BODY_MAX_LEN
        body = "\\\x1b[2J\u2028" + "x" * (cap - 7) + "\nCUT"
        sink = self._sink(httpserver, [(422, body)])
        with caplog.at_level(logging.WARNING, logger="baton"):
            await self._send(sink, 1)
        [warning] = self._refusal_warnings(caplog)
        assert "\\u{5c}\\u{1b}[2J\\u{2028}" in warning
        assert "x\\u{a} —" in warning, "the cap counts the raw text, so the newline is kept"
        assert "CUT" not in warning
        sink._closed = True


# =============================================================================
# An event that cannot be encoded
# =============================================================================


def _make_event_with_params(sequence_number: int, params: dict[str, Any]) -> ToolCallStartEvent:
    event = make_event(sequence_number)
    event.payload.params = params
    return event


_UNSERIALIZABLE = {"q": object()}
_LONE_SURROGATE = {"q": "half an emoji \ud83d"}


class TestAnUnencodableEventDoesNotBlockTheQueue:
    async def test_the_sender_drops_it_and_ships_what_follows(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        with caplog.at_level(logging.WARNING, logger="baton"):
            await sink.write(_make_event_with_params(1, _UNSERIALIZABLE))
            await sink.write(make_event(sequence_number=2))
            await sink.aclose()

        assert batches == [[2]]
        assert sink._sender.pending() == 0
        assert PROCESS_DROPS.total() == {"encode_failed": 1}
        dropped = [r for r in caplog.records if "has been dropped" in r.getMessage()]
        assert len(dropped) == 1
        assert "tool_call_start" in dropped[0].getMessage()
        assert "PydanticSerializationError" in dropped[0].getMessage()

    async def test_the_shutdown_flush_drops_it_and_ships_what_follows(
        self, httpserver: HTTPServer
    ) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._sender.enqueue(_make_event_with_params(1, _UNSERIALIZABLE))
        sink._sender.enqueue(make_event(sequence_number=2))

        sink._atexit_flush()

        assert batches == [[2]]
        assert sink._sender.pending() == 0
        assert PROCESS_DROPS.total() == {"encode_failed": 1}
        sink._closed = True

    async def test_a_lone_surrogate_is_dropped_and_does_not_stop_the_sender(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        with caplog.at_level(logging.WARNING, logger="baton"):
            await sink.write(_make_event_with_params(1, _LONE_SURROGATE))
            await sink.write(make_event(sequence_number=2))
            await sink.aclose()

        assert batches == [[2]]
        assert PROCESS_DROPS.total() == {"encode_failed": 1}
        assert "half an emoji" not in caplog.text

    async def test_a_lone_surrogate_does_not_stop_the_shutdown_flush(
        self, httpserver: HTTPServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        batches = _recording_batches(httpserver)
        sink = HttpSink(url=httpserver.url_for(""), api_key="k")
        sink._sender.enqueue(_make_event_with_params(1, _LONE_SURROGATE))
        sink._sender.enqueue(make_event(sequence_number=2))

        with caplog.at_level(logging.WARNING, logger="baton"):
            sink._atexit_flush()

        assert batches == [[2]]
        assert sink._sender.pending() == 0
        assert "half an emoji" not in caplog.text
        sink._closed = True

    async def test_dropping_it_does_not_close_an_open_circuit(self, httpserver: HTTPServer) -> None:
        sink = HttpSink(
            url=httpserver.url_for(""),
            api_key="k",
            circuit_breaker_threshold=1,
            circuit_breaker_reset_seconds=0.0,
        )
        sink._circuit.record_failure()
        sink._sender.enqueue(_make_event_with_params(1, _UNSERIALIZABLE))

        await sink.flush()

        assert sink._sender.pending() == 0
        assert sink._circuit._opened_at is not None
        await sink.aclose()

    async def test_an_unreachable_sink_url_is_not_blamed_on_the_event(self) -> None:
        sink = HttpSink(url="http://127.0.0.1:9/\x00", api_key="k", max_retries=0)
        sink._sender.enqueue(make_event())

        await sink.flush()

        assert sink._sender.pending() == 1
        assert PROCESS_DROPS.total() == {}
        sink._closed = True

    async def test_an_unencodable_api_key_is_not_blamed_on_the_event(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = HttpSink(url="http://127.0.0.1:9", api_key="k\u2603")
        sink._sender.enqueue(make_event())

        with caplog.at_level(logging.WARNING, logger="baton"):
            await sink.flush()
        [record] = caplog.records
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], UnicodeEncodeError)

        assert sink._sender.pending() == 1
        assert PROCESS_DROPS.total() == {}
        sink._closed = True


@pytest.fixture
def no_ambient_proxy(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in [n for n in os.environ if n.lower().endswith("_proxy")]:
        monkeypatch.delenv(name)
    monkeypatch.setattr(sinks_module, "_proxy_warned", False)
    return monkeypatch


def test_a_proxy_in_the_environment_is_used_unless_bypassed(
    no_ambient_proxy: pytest.MonkeyPatch,
) -> None:
    no_ambient_proxy.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    no_ambient_proxy.setenv("NO_PROXY", "skip.example")
    proxied = HttpSink(url="https://collector.example", api_key="k")._http_client
    direct = HttpSink(url="https://skip.example", api_key="k")._http_client
    assert isinstance(proxied, urllib3.ProxyManager)
    assert not isinstance(direct, urllib3.ProxyManager)


def test_a_proxy_without_a_scheme_is_http(no_ambient_proxy: pytest.MonkeyPatch) -> None:
    no_ambient_proxy.setenv("HTTPS_PROXY", "proxy.corp:3128")
    pool = HttpSink(url="https://collector.example", api_key="k")._http_client
    assert isinstance(pool, urllib3.ProxyManager)
    assert (pool.proxy.scheme, pool.proxy.host, pool.proxy.port) == ("http", "proxy.corp", 3128)


def test_all_proxy_is_used(no_ambient_proxy: pytest.MonkeyPatch) -> None:
    no_ambient_proxy.setenv("ALL_PROXY", "http://proxy.corp:3128")
    pool = HttpSink(url="https://collector.example", api_key="k")._http_client
    assert isinstance(pool, urllib3.ProxyManager)


def test_a_proxy_password_goes_in_proxy_authorization_and_not_the_url(
    no_ambient_proxy: pytest.MonkeyPatch,
) -> None:
    no_ambient_proxy.setenv("HTTPS_PROXY", "http://us%40er:s3cret@proxy.corp:3128")
    pool = HttpSink(url="https://collector.example", api_key="k")._http_client
    assert isinstance(pool, urllib3.ProxyManager)
    expected = urllib3.make_headers(proxy_basic_auth="us@er:s3cret")["proxy-authorization"]
    assert pool.proxy_headers["proxy-authorization"] == expected
    assert "s3cret" not in str(pool.proxy)


@pytest.mark.parametrize("value", ["socks5://proxy.corp:1080", "http://[proxy.corp"])
def test_a_bad_proxy_never_stops_the_sink_and_warns_once(
    no_ambient_proxy: pytest.MonkeyPatch, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    no_ambient_proxy.setenv("HTTPS_PROXY", value)
    with caplog.at_level(logging.WARNING, logger="baton"):
        pools = [HttpSink(url="https://collector.example", api_key="k")._http_client for _ in "ab"]
    assert not any(isinstance(p, urllib3.ProxyManager) for p in pools)
    assert [r.getMessage() for r in caplog.records if "proxy" in r.getMessage()] == [
        caplog.records[0].getMessage()
    ]
    assert "proxy.corp" not in caplog.text


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("0", 1.0),
        ("30", 30.0),
        (" 30 ", 30.0),
        ("120", 60.0),
        ("0.2", None),
        ("1.5", None),
        ("-3", None),
        ("soon", None),
        ("inf", None),
        ("nan", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        (None, None),
    ],
)
def test_retry_after_is_whole_seconds_from_1_to_60(
    header: str | None, seconds: float | None
) -> None:
    headers = {} if header is None else {"Retry-After": header}
    assert sinks_module._retry_after_seconds(headers) == seconds


async def test_retry_after_0_waits_a_second_rather_than_looping(httpserver: HTTPServer) -> None:
    times: list[float] = []

    def handler(request: Any) -> Response:
        times.append(time.monotonic())
        if len(times) == 1:
            return Response("", status=429, headers={"Retry-After": "0"})
        return Response("", status=200)

    httpserver.expect_request(BATCH, method="POST").respond_with_handler(handler)
    sink = HttpSink(url=httpserver.url_for(""), api_key="k", flush_interval_seconds=0.01)
    await sink.write(make_event())
    await until(lambda: len(times) >= 2)
    assert times[1] - times[0] >= 1.0
    await sink.aclose()
    assert len(times) == 2


@pytest.mark.parametrize(("header", "failures"), [("1", 0), ("soon", 1)])
async def test_only_a_readable_retry_after_spares_the_circuit(header: str, failures: int) -> None:
    sink = fake_sink(lambda path, body: Answer(429, headers={"Retry-After": header}))
    await sink.write(make_event())
    await sink.flush()
    assert sink._circuit._failures == failures
    assert sink._sender.pending() == 1
    await sink.aclose()


@pytest.mark.parametrize("interval", [float("inf"), float("nan"), -1, 1e12, "5"])
def test_a_bad_flush_interval_falls_back_to_the_default_with_a_warning(
    interval: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="baton"):
        sink = HttpSink(
            url="http://collector.example", api_key="k", flush_interval_seconds=interval
        )
    assert sink._sender.flush_interval == sinks_module.FLUSH_INTERVAL_SECONDS
    assert any("flush_interval_seconds" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_forked_child_gets_its_own_connection_pool() -> None:
    sink = HttpSink(url="http://collector.example", api_key="k")
    parent_pool = sink._sender.pool
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        sink._sender.enqueue(make_event())
        os.write(write, b"1" if sink._sender.pool is not parent_pool else b"0")
        os._exit(0)
    os.close(write)
    os.waitpid(pid, 0)
    assert os.read(read, 1) == b"1"
    assert sink._sender.pool is parent_pool
    sink._closed = True
