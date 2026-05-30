"""EventEmitter — bounded local buffer + async POST + retry + circuit breaker.

Per SPEC §11.2 + CHARTER OD-7 (thin-emit SDK): the emitter MUST NOT block the
vendor's hot path on Console availability. Failures are bounded — buffer
overflow drops oldest events (with one ``UserWarning``); network failures
retry with backoff; circuit breaker opens after consecutive failures so the
SDK doesn't pile retries on a known-bad endpoint.

Sequence numbers are caller-supplied (the middleware tracks per-session
counters). The emitter is dumb about ordering: it sends what it's given,
oldest-first.
"""

from __future__ import annotations

import asyncio
import warnings
from collections import deque
from time import monotonic
from types import TracebackType
from typing import Self

import httpx

from baton.events import Event


class _CircuitBreaker:
    """Two-state breaker: closed (normal) or open (skip with reset window).

    Threshold = consecutive failures before opening. Reset window allows a
    tentative attempt; on success the breaker closes, on failure it stays
    open and the window restarts.
    """

    def __init__(self, threshold: int, reset_seconds: float) -> None:
        self._threshold = threshold
        self._reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None

    def can_request(self) -> bool:
        if self._opened_at is None:
            return True
        if monotonic() - self._opened_at >= self._reset_seconds:
            return True  # window expired — half-open behavior
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = monotonic()


class EventEmitter:
    """Bounded buffer + async POST + retry + circuit breaker."""

    def __init__(
        self,
        *,
        console_url: str,
        api_key: str,
        buffer_size: int = 1000,
        request_timeout_seconds: float = 1.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.1,
        backoff_max_seconds: float = 5.0,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_reset_seconds: float = 30.0,
        _http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._console_url = console_url.rstrip("/")
        self._api_key = api_key
        self._buffer: deque[Event] = deque(maxlen=buffer_size)
        self._overflow_warned = False
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._circuit = _CircuitBreaker(circuit_breaker_threshold, circuit_breaker_reset_seconds)
        self._http_client = _http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout_seconds),
        )
        self._owns_client = _http_client is None
        self._flush_lock = asyncio.Lock()
        self._drain_task: asyncio.Task[None] | None = None
        self._closed = False

    # =========================================================================
    # Public API
    # =========================================================================

    async def emit(self, event: Event) -> None:
        """Enqueue an event for sending. Never blocks on Console availability.

        Spawns a background drain task on first emit (idempotent — if one is
        already running, do nothing; the running task will see the new event).

        On buffer overflow: oldest event dropped, ``UserWarning`` emitted once.
        """
        if self._closed:
            raise RuntimeError("EventEmitter is closed")
        self._enqueue(event)
        self._ensure_drain_running()

    async def flush(self) -> None:
        """Wait for the current background drain to complete, then drain any
        remaining events. Returns when buffer is empty OR the circuit is open
        (events stay enqueued for next flush attempt)."""
        if self._closed:
            return
        # Wait for the in-flight drain task (if any) to finish.
        if self._drain_task is not None and not self._drain_task.done():
            await self._drain_task
        # Final pass under the lock — picks up any events that arrived after
        # the drain task finished but before flush() acquired the lock.
        async with self._flush_lock:
            await self._drain_locked()

    async def aclose(self) -> None:
        """Flush pending events; close the HTTP client.

        After close, subsequent ``emit`` calls raise ``RuntimeError``."""
        if self._closed:
            return
        await self.flush()
        self._closed = True
        if self._owns_client:
            await self._http_client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # =========================================================================
    # Internal — enqueue + drain
    # =========================================================================

    def _enqueue(self, event: Event) -> None:
        # deque(maxlen=...) auto-drops oldest on append when full; we check
        # BEFORE appending so the UserWarning fires before the drop happens.
        assert self._buffer.maxlen is not None
        if len(self._buffer) >= self._buffer.maxlen:
            if not self._overflow_warned:
                warnings.warn(
                    f"Baton event buffer full ({self._buffer.maxlen}); "
                    "oldest events dropped. Further overflows will be silent.",
                    UserWarning,
                    stacklevel=3,
                )
                self._overflow_warned = True
        self._buffer.append(event)

    # Test hook — enables overflow tests to populate the buffer without
    # racing against the background flush loop. Production code uses ``emit``.
    def _enqueue_for_test(self, event: Event) -> None:
        self._enqueue(event)

    def _ensure_drain_running(self) -> None:
        """Spawn a background drain task if one isn't already running. Standard
        telemetry-SDK pattern (Sentry / OpenTelemetry / PostHog / Datadog all
        use a variant of this): emit() returns immediately; a background task
        does the I/O so the producer never waits on the network."""
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._background_drain())

    async def _background_drain(self) -> None:
        """Drain the buffer under the lock, then exit. If new events arrive
        while this task is draining, ``_drain_locked``'s inner while loop sees
        them. If events arrive after this task releases the lock, the next
        ``emit`` call's ``_ensure_drain_running`` spawns a fresh task."""
        async with self._flush_lock:
            await self._drain_locked()

    async def _drain_locked(self) -> None:
        """Drain the buffer one event at a time. Lock held by caller (``flush``)."""
        while self._buffer:
            if not self._circuit.can_request():
                return  # circuit open; bail; events stay in buffer
            event = self._buffer[0]
            outcome = await self._send_with_retry(event)
            if outcome == "success":
                self._buffer.popleft()
                self._circuit.record_success()
            elif outcome == "permanent_failure":
                # 4xx (auth/malformed/etc.): event is unsendable; drop it
                self._buffer.popleft()
                self._circuit.record_success()  # endpoint is healthy; just bad data
            else:  # "transient_failure"
                self._circuit.record_failure()
                return  # bail; event stays in buffer; try later

    async def _send_with_retry(self, event: Event) -> str:
        """Returns "success", "permanent_failure", or "transient_failure"."""
        url = f"{self._console_url}/v0/events"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        body = event.model_dump(mode="json")

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http_client.post(url, json=body, headers=headers)
                status = response.status_code
                if 200 <= status < 300:
                    return "success"
                # 4xx (except 429) is permanent — auth, malformed, etc.
                if 400 <= status < 500 and status != 429:
                    return "permanent_failure"
                # 5xx and 429: transient; fall through to retry
            except httpx.HTTPError:
                # httpx.TimeoutException inherits from HTTPError, so this catches both.
                pass  # transient; retry

            if attempt < self._max_retries:
                backoff = min(self._backoff_base * (2**attempt), self._backoff_max)
                await asyncio.sleep(backoff)

        return "transient_failure"
