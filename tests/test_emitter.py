"""Tests for the EventEmitter per SPEC §11.2 + CHARTER OD-7.

Spec-first, failing-test-first: this file is written BEFORE
``src/baton/emitter.py``.

Coverage: basic POST to /v0/events; bearer auth; JSON body shape; bounded
buffer with overflow + UserWarning (emitted once); retry-with-backoff on
transient failure; circuit breaker after consecutive failures; flush() drains;
aclose() flushes + closes.

Uses pytest-httpserver for a real in-process HTTP server (no mocks).
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.emitter import EventEmitter
from baton.events import ToolCallStartEvent, ToolCallStartPayload


def _make_event(sequence_number: int = 1) -> ToolCallStartEvent:
    return ToolCallStartEvent(
        tenant_id="ten_test",
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
        emitter = EventEmitter(console_url=httpserver.url_for(""), api_key="test-key")
        await emitter.emit(_make_event())
        await emitter.flush()
        await emitter.aclose()
        httpserver.check_assertions()

    async def test_bearer_auth_header(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(
            "/v0/events",
            method="POST",
            headers={"Authorization": "Bearer my-secret-key"},
        ).respond_with_data("", status=201)
        emitter = EventEmitter(console_url=httpserver.url_for(""), api_key="my-secret-key")
        await emitter.emit(_make_event())
        await emitter.flush()
        await emitter.aclose()
        httpserver.check_assertions()

    async def test_body_is_event_json(self, httpserver: HTTPServer) -> None:
        captured: list[dict[str, Any]] = []

        def handler(request: Any) -> Response:
            captured.append(request.get_json())
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
        emitter = EventEmitter(console_url=httpserver.url_for(""), api_key="k")
        event = _make_event(sequence_number=42)
        await emitter.emit(event)
        await emitter.flush()
        await emitter.aclose()

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
        emitter = EventEmitter(console_url=httpserver.url_for(""), api_key="k")
        for i in range(5):
            await emitter.emit(_make_event(sequence_number=i + 1))
        await emitter.flush()
        await emitter.aclose()

        assert seqs == [1, 2, 3, 4, 5]


# =============================================================================
# Bounded buffer + overflow
# =============================================================================


class TestBufferOverflow:
    async def test_overflow_drops_oldest_with_userwarning(self, httpserver: HTTPServer) -> None:
        """Per SPEC §11.2 + CHARTER OD-7: buffer is bounded; on overflow,
        oldest events are dropped and a UserWarning is emitted."""
        # Configure ingest to hang so events accumulate in the buffer.
        # We won't flush during the test; we just check the buffer state.
        emitter = EventEmitter(
            console_url=httpserver.url_for(""),
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
                emitter._enqueue_for_test(_make_event(sequence_number=i + 1))

        # 2 events dropped (capacity=3, pushed 5)
        assert len(emitter._buffer) == 3
        # Oldest dropped: sequence_numbers in buffer are now 3, 4, 5
        seqs_in_buffer = [ev.sequence_number for ev in emitter._buffer]
        assert seqs_in_buffer == [3, 4, 5]
        # UserWarning emitted
        warning_messages = [str(w.message) for w in caught if w.category is UserWarning]
        assert any("buffer" in msg.lower() and "drop" in msg.lower() for msg in warning_messages)

        await emitter.aclose()

    async def test_userwarning_emitted_only_once(self, httpserver: HTTPServer) -> None:
        emitter = EventEmitter(
            console_url=httpserver.url_for(""),
            api_key="k",
            buffer_size=2,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for i in range(10):
                emitter._enqueue_for_test(_make_event(sequence_number=i + 1))

        warning_count = sum(
            1 for w in caught if w.category is UserWarning and "drop" in str(w.message).lower()
        )
        assert warning_count == 1, f"expected exactly one warning, got {warning_count}"
        await emitter.aclose()


# =============================================================================
# Auto-flush on emit (background drain task)
# =============================================================================


class TestAutoFlush:
    """emit() spawns a background drain task; events ship without an explicit
    flush() call. This is the standard telemetry-SDK pattern (Sentry, OTel,
    PostHog, ddtrace all do a variant of this)."""

    async def test_emit_alone_eventually_drains(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        emitter = EventEmitter(console_url=httpserver.url_for(""), api_key="k")
        await emitter.emit(_make_event())

        # No explicit flush() call. Wait for the background drain task to finish.
        assert emitter._drain_task is not None
        await emitter._drain_task

        assert len(emitter._buffer) == 0
        httpserver.check_assertions()
        await emitter.aclose()

    async def test_rapid_emits_share_single_drain_task(self, httpserver: HTTPServer) -> None:
        """Multiple rapid emits don't spawn redundant drain tasks. The
        in-flight task continues to see new events as they're enqueued."""
        seen: list[int] = []

        def handler(request: Any) -> Response:
            seen.append(request.get_json()["sequence_number"])
            return Response("", status=201)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)

        emitter = EventEmitter(console_url=httpserver.url_for(""), api_key="k")
        # Emit 5 events rapidly; the drain task should pick them all up
        for i in range(5):
            await emitter.emit(_make_event(sequence_number=i + 1))

        # Wait for whichever drain task is current to finish
        assert emitter._drain_task is not None
        await emitter._drain_task
        # Some events may have arrived during the first task's exit window;
        # call flush() to drain any stragglers.
        await emitter.flush()

        assert sorted(seen) == [1, 2, 3, 4, 5]
        await emitter.aclose()

    async def test_aclose_drains_pending_events(self, httpserver: HTTPServer) -> None:
        """aclose() waits for the background drain to complete, including any
        events emitted right before close."""
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        emitter = EventEmitter(console_url=httpserver.url_for(""), api_key="k")
        await emitter.emit(_make_event())
        # No flush — just close
        await emitter.aclose()
        # Should have drained on close
        assert len(emitter._buffer) == 0
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

        emitter = EventEmitter(
            console_url=httpserver.url_for(""),
            api_key="k",
            max_retries=3,
            backoff_base_seconds=0.001,
        )
        await emitter.emit(_make_event())
        await emitter.flush()
        await emitter.aclose()

        # Buffer should be empty — event eventually sent
        assert len(emitter._buffer) == 0
        httpserver.check_assertions()

    async def test_does_not_retry_4xx(self, httpserver: HTTPServer) -> None:
        """4xx (except 429) are permanent; don't retry."""
        call_count = 0

        def handler(_request: Any) -> Response:
            nonlocal call_count
            call_count += 1
            return Response("bad request", status=400)

        httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)

        emitter = EventEmitter(
            console_url=httpserver.url_for(""),
            api_key="k",
            max_retries=3,
            backoff_base_seconds=0.001,
        )
        await emitter.emit(_make_event())
        await emitter.flush()
        await emitter.aclose()

        # 4xx is permanent: one attempt, no retry. Event is dropped from buffer.
        assert call_count == 1
        assert len(emitter._buffer) == 0


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

        emitter = EventEmitter(
            console_url=httpserver.url_for(""),
            api_key="k",
            max_retries=1,
            backoff_base_seconds=0.001,
            circuit_breaker_threshold=3,
            circuit_breaker_reset_seconds=60.0,
        )

        # Send 5 events; only the first 3 should trigger HTTP attempts
        # (max_retries=1 means 2 attempts each before counting as a failure).
        for _ in range(5):
            await emitter.emit(_make_event())
            await emitter.flush()

        # 3 consecutive failed flushes x 2 attempts each = 6 HTTP calls
        # then circuit opens; remaining 2 emits skip HTTP entirely.
        assert call_count == 6
        # Events stay in buffer (circuit open; can't send)
        assert len(emitter._buffer) == 5
        await emitter.aclose()


# =============================================================================
# flush() + aclose()
# =============================================================================


class TestLifecycle:
    async def test_flush_drains_buffer(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        emitter = EventEmitter(console_url=httpserver.url_for(""), api_key="k")
        for i in range(10):
            await emitter.emit(_make_event(sequence_number=i + 1))
        await emitter.flush()
        assert len(emitter._buffer) == 0
        await emitter.aclose()

    async def test_aclose_flushes_and_closes(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
        emitter = EventEmitter(console_url=httpserver.url_for(""), api_key="k")
        await emitter.emit(_make_event())
        await emitter.aclose()
        assert len(emitter._buffer) == 0
        # Subsequent emit raises (closed)
        with pytest.raises(RuntimeError):
            await emitter.emit(_make_event(sequence_number=2))
