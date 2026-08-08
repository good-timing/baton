"""Shared machinery for the perf test harness and the standalone soak script
(``scripts/soak.py``).

Deliberately has NO pytest import — ``scripts/soak.py`` imports this module
directly, outside a pytest session, so the CI-gated fast tests and the heavy
soak share one implementation rather than diverging.

Provides:

- ``FakeCollector`` — a stdlib-only fake Console ingest endpoint with a
  runtime-switchable health mode (healthy / slow / erroring), for exercising
  ``HttpSink`` against collector failure states without a real vendor or a
  real Console. Built on ``http.server``, not ``pytest_httpserver`` — that's
  a pytest-only fixture and can't be constructed outside a test session.
- ``dead_url()`` — a URL that deterministically refuses connections.
- ``NullSink`` — a zero-I/O ``Sink`` that isolates wrap-layer CPU cost
  (scrub + serialize) from network variance, for the overhead-budget test.
- Timing helpers: ``timed_async_calls`` / ``timed_calls`` / ``percentile`` /
  ``median``.
- ``mcp_session`` / ``fastmcp_session`` / ``library_session`` — async (or,
  for the sync library client, plain) context managers that each stand up
  one capture path against a caller-supplied sink and yield a callable that
  drives one tool call through the REAL wrapped hot path (not a stub of it).
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from time import monotonic
from typing import Any

from baton.events import Event
from baton.sinks import Sink

# =============================================================================
# FakeCollector — stdlib-only fake Console ingest endpoint
# =============================================================================


class FakeCollector:
    """A minimal fake Console collector, health-switchable at runtime via
    ``.mode``. Threaded so concurrent sinks/sessions (as the soak script
    drives) don't serialize behind one slow request."""

    def __init__(self) -> None:
        self.mode: str = "healthy"  # "healthy" | "slow" | "erroring"
        self.slow_seconds: float = 3.0
        self.received: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        collector = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                mode = collector.mode
                if mode == "slow":
                    time.sleep(collector.slow_seconds)
                if mode == "erroring":
                    self.send_response(500)
                    self.end_headers()
                    return
                with collector._lock:
                    collector.received.append(json.loads(body))
                self.send_response(201)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass  # silence stdlib's per-request stderr log line

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


def dead_url() -> str:
    """A URL that refuses connections deterministically: bind then
    immediately close a port, rather than relying on an OS-level TCP hang
    timeout against a never-bound address (which varies by platform)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# =============================================================================
# NullSink — zero-I/O sink for isolating wrap-layer CPU cost
# =============================================================================


class NullSink(Sink):
    """Accepts events with no I/O. Used by the overhead-budget test to
    measure scrub+serialize CPU cost without network variance riding along."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._closed = False

    async def write(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("NullSink is closed")
        self.events.append(event)

    async def flush(self) -> None:
        return None

    async def aclose(self) -> None:
        self._closed = True


# =============================================================================
# Timing helpers
# =============================================================================


async def timed_async_calls(
    fn: Callable[[], Awaitable[Any]], n: int, *, warmup: int = 0
) -> list[float]:
    """Run ``fn()`` ``warmup`` times (discarded — see the overhead-budget
    test's surface_snapshot note), then ``n`` times, returning each call's
    wall-clock duration in seconds."""
    for _ in range(warmup):
        await fn()
    samples: list[float] = []
    for _ in range(n):
        start = monotonic()
        await fn()
        samples.append(monotonic() - start)
    return samples


def timed_calls(fn: Callable[[], Any], n: int, *, warmup: int = 0) -> list[float]:
    """Sync equivalent of ``timed_async_calls``, for the sync library ``Client``."""
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(n):
        start = monotonic()
        fn()
        samples.append(monotonic() - start)
    return samples


def percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    k = (len(ordered) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def median(samples: list[float]) -> float:
    return percentile(samples, 50)


# =============================================================================
# Bounded teardown — see test_nonblocking_hotpath.py's module docstring for
# the full "known trap" writeup: aclose()/flush() await the drain directly
# and are NOT bounded by HttpSink's shutdown_flush_timeout_seconds (that
# kwarg only guards the atexit fallback path). TIGHT_SINK_KWARGS keeps
# teardown fast by construction; bounded_aclose is defense in depth on top.
#
# circuit_breaker_threshold is deliberately HIGH (not tightened like the
# other kwargs) — verified empirically (deliberately patching write() to
# await the drain inline) that a low threshold opens the breaker within the
# first 1-2 calls, and once open, _drain_locked short-circuits to an
# instant no-op BEFORE attempting HTTP — masking the exact "write() blocks
# on the network" regression this kwarg set exists to help catch, for every
# call after the breaker opens (which, at a low threshold, is nearly all of
# them). max_retries=0 + a short request_timeout_seconds keeps teardown
# fast regardless — flush()/aclose() stop draining after the FIRST failed
# attempt (see HttpSink._drain_locked: it returns on any non-success
# outcome, it does not loop the whole buffer), so a high threshold doesn't
# reintroduce the teardown-hang trap.
# =============================================================================

TIGHT_SINK_KWARGS: dict[str, Any] = {
    "max_retries": 0,
    "backoff_base_seconds": 0.01,
    "backoff_max_seconds": 0.01,
    "request_timeout_seconds": 0.2,
    "circuit_breaker_threshold": 10_000,
    "circuit_breaker_reset_seconds": 0.5,
}


async def bounded_aclose(closeable: Any, *, timeout: float = 2.0) -> None:
    """Teardown helper for anything with an async ``aclose()`` whose backing
    collector may be unhealthy. Always use this instead of a bare
    ``await x.aclose()`` in the crown-jewel tests."""
    try:
        await asyncio.wait_for(closeable.aclose(), timeout=timeout)
    except TimeoutError:
        pass


# =============================================================================
# Per-capture-path drivers
# =============================================================================


@asynccontextmanager
async def mcp_session(
    sink: Sink, *, vendor_id: str = "harness-vendor"
) -> AsyncIterator[Callable[[], Awaitable[Any]]]:
    """Stand up an mcp-adapter FastMCP server with install_baton applied,
    yielding an async callable that drives one tool call through the real
    wrapped hot path."""
    from baton.integrations.mcp import VendorConfig, install_baton
    from baton.integrations.mcp._compat import MCPServerClass as FastMCP

    mcp = FastMCP("harness-mcp")

    @mcp.tool()
    def ping(x: int = 1) -> dict[str, Any]:
        return {"ok": True, "x": x}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id=vendor_id,
            vendor_display_name="Harness Vendor",
            consent_token="ct_harness",
            sink=sink,
        ),
    )

    async def call() -> Any:
        return await mcp.call_tool("ping", {"x": 1})

    try:
        yield call
    finally:
        await bounded_aclose(handle)


@asynccontextmanager
async def fastmcp_session(
    sink: Sink, *, vendor_id: str = "harness-vendor"
) -> AsyncIterator[Callable[[], Awaitable[Any]]]:
    """Stand up a standalone-fastmcp-adapter server with install_baton
    applied, yielding an async callable that drives one tool call through
    the real wrapped hot path via an in-process fastmcp ``Client``."""
    from fastmcp import Client, FastMCP

    from baton.integrations.fastmcp import VendorConfig, install_baton

    mcp = FastMCP("harness-mcp")

    @mcp.tool()
    def ping(x: int = 1) -> dict[str, Any]:
        return {"ok": True, "x": x}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id=vendor_id,
            vendor_display_name="Harness Vendor",
            consent_token="ct_harness",
            sink=sink,
        ),
    )

    try:
        async with Client(mcp) as client:

            async def call() -> Any:
                return await client.call_tool("ping", {"x": 1})

            yield call
    finally:
        await bounded_aclose(handle)


@contextmanager
def library_session(
    sink: Sink, *, vendor_id: str = "harness-vendor"
) -> Iterator[Callable[[], Any]]:
    """Stand up a sync library-API ``Client`` against the given sink,
    yielding a sync callable that drives one traced call through the real
    hot path (background-thread bridge included)."""
    from baton import Client

    client = Client(vendor_id=vendor_id, consent_token="ct_harness", sink=sink)

    def call() -> Any:
        with client.trace(tool_name="ping", params={"x": 1}) as trace:
            trace.observed({"ok": True})
        return None

    try:
        yield call
    finally:
        # Bounded by construction when the sink was built with
        # TIGHT_SINK_KWARGS (see module docstring) — Client.close() has no
        # timeout parameter of its own to wrap externally.
        client.close()
