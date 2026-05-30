"""Sinks — where Baton events go after the SDK captures them.

A ``Sink`` is the egress side of the SDK. The capture surface (library API
``Client`` / ``AsyncClient``, or the MCP middleware) hands fully-formed
``Event`` envelopes to a sink; the sink decides what to do with them.

Four sinks ship in core:

- ``StdoutSink`` — JSONL to a file-like (default stderr). Zero config; useful
  for local development and the zero-dependency examples.
- ``FileSink`` — JSONL append to a path.
- ``HttpSink`` — POST to any HTTP endpoint with bearer auth; bounded buffer,
  retry, circuit breaker. This is the contract a Console or any compatible
  collector consumes.
- ``MultiSink`` — fan out to a list of sinks.

The Sink protocol is intentionally small (``write`` + ``flush`` + ``aclose``).
Backpressure, retry, and durability are sink-specific concerns: ``HttpSink``
buffers and retries because networks fail; ``StdoutSink`` and ``FileSink``
either succeed synchronously or raise.
"""

from __future__ import annotations

import asyncio
import json
import sys
import warnings
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import IO, Self

import httpx

from baton.events import Event


class Sink(ABC):
    """A destination for emitted events. Implementations choose their own
    backpressure / retry / durability semantics."""

    @abstractmethod
    async def write(self, event: Event) -> None:
        """Hand one event to the sink. Returns when the sink has accepted it
        (which may mean buffered, not yet shipped). MUST NOT block the
        producer on slow downstream destinations."""

    @abstractmethod
    async def flush(self) -> None:
        """Wait for any buffered events to reach their destination (or be
        dropped per the sink's policy). Noop for synchronous sinks."""

    @abstractmethod
    async def aclose(self) -> None:
        """Flush + release resources. After ``aclose``, ``write`` raises."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


# =============================================================================
# StdoutSink — JSONL to a text stream (default: stderr)
# =============================================================================


class StdoutSink(Sink):
    """Write one JSON envelope per line to a text stream.

    Defaults to ``sys.stderr`` because the MCP stdio transport reserves
    ``sys.stdout`` for JSON-RPC framing — emitting capture events on stdout
    would corrupt the protocol stream. Library-API users (no MCP) can pass
    ``stream=sys.stdout`` if they prefer.
    """

    def __init__(self, *, stream: IO[str] | None = None) -> None:
        self._stream: IO[str] = stream if stream is not None else sys.stderr
        self._closed = False

    async def write(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("StdoutSink is closed")
        line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
        self._stream.write(line + "\n")
        self._stream.flush()

    async def flush(self) -> None:
        if self._closed:
            return
        self._stream.flush()

    async def aclose(self) -> None:
        self._closed = True
        # Don't close the stream — we don't own stderr/stdout.


# =============================================================================
# FileSink — JSONL append to a path
# =============================================================================


class FileSink(Sink):
    """Append one JSON envelope per line to a file.

    Opens the file on first ``write`` (so constructing a FileSink without
    using it doesn't create an empty file). Uses line-buffered text mode so
    each event is visible to ``tail -f`` without an explicit flush.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._file: IO[str] | None = None
        self._closed = False

    async def write(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("FileSink is closed")
        if self._file is None:
            self._file = self._path.open("a", buffering=1, encoding="utf-8")
        line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
        self._file.write(line + "\n")

    async def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._file is not None:
            self._file.close()
            self._file = None


# =============================================================================
# HttpSink — POST + bearer + bounded buffer + retry + circuit breaker
# =============================================================================


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
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = monotonic()


class HttpSink(Sink):
    """POST events to an HTTP endpoint. Bounded buffer + retry + circuit breaker.

    This is the contract a Console or any compatible collector consumes:
    ``POST {url}/v0/events`` with ``Authorization: Bearer {api_key}`` and a
    JSON body matching the SPEC §11.4 envelope.

    Failures are bounded — buffer overflow drops oldest events (with one
    ``UserWarning``); network failures retry with backoff; circuit breaker
    opens after consecutive failures so the SDK doesn't pile retries on a
    known-bad endpoint.
    """

    def __init__(
        self,
        url: str,
        *,
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
        self._url = url.rstrip("/")
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

    async def write(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("HttpSink is closed")
        self._enqueue(event)
        self._ensure_drain_running()

    async def flush(self) -> None:
        if self._closed:
            return
        if self._drain_task is not None and not self._drain_task.done():
            await self._drain_task
        async with self._flush_lock:
            await self._drain_locked()

    async def aclose(self) -> None:
        if self._closed:
            return
        await self.flush()
        self._closed = True
        if self._owns_client:
            await self._http_client.aclose()

    # =========================================================================
    # Internal — enqueue + drain
    # =========================================================================

    def _enqueue(self, event: Event) -> None:
        assert self._buffer.maxlen is not None
        if len(self._buffer) >= self._buffer.maxlen:
            if not self._overflow_warned:
                warnings.warn(
                    f"Baton HTTP sink buffer full ({self._buffer.maxlen}); "
                    "oldest events dropped. Further overflows will be silent.",
                    UserWarning,
                    stacklevel=3,
                )
                self._overflow_warned = True
        self._buffer.append(event)

    # Test hook — see test_sinks.py for the overflow-semantics tests.
    def _enqueue_for_test(self, event: Event) -> None:
        self._enqueue(event)

    def _ensure_drain_running(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._background_drain())

    async def _background_drain(self) -> None:
        async with self._flush_lock:
            await self._drain_locked()

    async def _drain_locked(self) -> None:
        while self._buffer:
            if not self._circuit.can_request():
                return
            event = self._buffer[0]
            outcome = await self._send_with_retry(event)
            if outcome == "success":
                self._buffer.popleft()
                self._circuit.record_success()
            elif outcome == "permanent_failure":
                self._buffer.popleft()
                self._circuit.record_success()
            else:
                self._circuit.record_failure()
                return

    async def _send_with_retry(self, event: Event) -> str:
        url = f"{self._url}/v0/events"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        body = event.model_dump(mode="json")

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http_client.post(url, json=body, headers=headers)
                status = response.status_code
                if 200 <= status < 300:
                    return "success"
                if 400 <= status < 500 and status != 429:
                    return "permanent_failure"
            except httpx.HTTPError:
                pass

            if attempt < self._max_retries:
                backoff = min(self._backoff_base * (2**attempt), self._backoff_max)
                await asyncio.sleep(backoff)

        return "transient_failure"


# =============================================================================
# MultiSink — fan out to a list
# =============================================================================


class MultiSink(Sink):
    """Fan out each event to every sink in the list.

    Useful during development: ``MultiSink([StdoutSink(), HttpSink(...)])``
    gives you a live view of what's being shipped alongside the real egress.

    A failure in one sink does not prevent the others from being called.
    Exceptions are aggregated and re-raised after all sinks have been tried.
    """

    def __init__(self, sinks: list[Sink]) -> None:
        if not sinks:
            raise ValueError("MultiSink requires at least one sink")
        self._sinks = sinks

    async def write(self, event: Event) -> None:
        await self._fan_out(lambda s: s.write(event))

    async def flush(self) -> None:
        await self._fan_out(lambda s: s.flush())

    async def aclose(self) -> None:
        await self._fan_out(lambda s: s.aclose())

    async def _fan_out(self, op: object) -> None:
        results = await asyncio.gather(
            *(op(s) for s in self._sinks),  # type: ignore[operator]
            return_exceptions=True,
        )
        # Narrow to Exception (not BaseException) — never aggregate
        # CancelledError / KeyboardInterrupt into a sink-failure group.
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("MultiSink fan-out failures", errors)


__all__ = [
    "FileSink",
    "HttpSink",
    "MultiSink",
    "Sink",
    "StdoutSink",
]
