"""Sinks — where Baton events go after the SDK captures them.

A ``Sink`` is the egress side of the SDK. The capture surface (library API
``Client`` / ``AsyncClient``, or the MCP middleware) hands fully-formed
``Event`` envelopes to a sink; the sink decides what to do with them.

Four sinks ship in core:

- ``StdoutSink`` — JSONL to a file-like (default stderr). Zero config; useful
  for local development and the zero-dependency examples.
- ``FileSink`` — JSONL append to a path.
- ``HttpSink`` — POST batches to any HTTP endpoint with bearer auth, from its
  own thread; bounded buffer, retry, circuit breaker. This is the contract a Console or any compatible
  collector consumes.
- ``MultiSink`` — fan out to a list of sinks.

The Sink protocol is intentionally small (``write`` + ``flush`` + ``aclose``).
Backpressure, retry, and durability are sink-specific concerns: ``HttpSink``
buffers and retries because networks fail; ``StdoutSink`` and ``FileSink``
either succeed synchronously or raise.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import inspect
import itertools
import json
import logging
import math
import os
import sys
import threading
import unicodedata
import urllib.parse
import urllib.request
import warnings
import weakref
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import IO, TYPE_CHECKING, Any, Literal, Self, cast

from baton._state import PROCESS_DROPS, PROCESS_SEQUENCE
from baton.events import ClientReportEvent, ClientReportPayload, Event

if TYPE_CHECKING:
    import urllib3


def _require_urllib3() -> Any:
    """Imported on first use, so ``import baton`` does not load it when no
    ``HttpSink`` is built."""
    try:
        import urllib3
    except ImportError as e:  # pragma: no cover - a broken install
        raise ImportError(
            "HttpSink needs urllib3, which baton-sdk depends on, and this "
            "environment does not have it. Reinstall with: pip install baton-sdk"
        ) from e
    return urllib3


class SinkClosedError(RuntimeError):
    pass


# By id(), since a vendor's sink may be unhashable; the weakref drops the
# entry when the sink dies, before its id can be reused. A sink with no weakref
# support stays by id, bounded: a later sink at that id may miss its warning.
_warned_closed: dict[int, weakref.ref[Sink] | None] = {}
_WARNED_WITHOUT_WEAKREF_MAX = 1024
_warned_without_weakref = 0


def _first_closed_warning(sink: Sink) -> bool:
    global _warned_without_weakref
    key = id(sink)
    if key in _warned_closed:
        return False
    try:
        _warned_closed[key] = weakref.ref(sink, lambda _: _warned_closed.pop(key, None))
    except TypeError:
        if _warned_without_weakref < _WARNED_WITHOUT_WEAKREF_MAX:
            _warned_without_weakref += 1
            _warned_closed[key] = None
    return True


async def write_unless_closed(sink: Sink, event: Event, logger: logging.Logger) -> bool:
    """``sink.write``; False when the sink is closed, which counts the event
    ``after_close`` and warns once per sink. Any other error propagates."""
    try:
        await sink.write(event)
    except SinkClosedError:
        PROCESS_DROPS.add("after_close")
        if _first_closed_warning(sink):
            logger.warning("baton: the sink is closed; further events are dropped.")
        return False
    return True


async def safe_write(sink: Sink, event: Event, logger: logging.Logger) -> None:
    """Fail-open wrapper around ``sink.write``.

    SDK middleware sits BEFORE the vendor's tool handler. A raise from
    ``sink.write`` (closed sink, transport error, ``warnings.filterwarnings
    ("error")`` promoting an overflow ``UserWarning``, ``BrokenPipeError``
    from ``StdoutSink``, etc.) would otherwise propagate up and break the
    vendor's tool call — making Baton instrumentation look like a vendor
    bug. SPEC §11.2 mandates fail-open at the capture boundary; this wrapper
    enforces it.

    Catches ``Exception``, NOT ``BaseException`` — ``KeyboardInterrupt``
    and ``SystemExit`` must still propagate for clean shutdown.
    """
    try:
        await write_unless_closed(sink, event, logger)
    except Exception:
        PROCESS_DROPS.add("write_failed")
        logger.exception("baton: sink.write failed; event dropped, tool call continues")


async def safe_emit(sink: Sink, build: Callable[[], Event], logger: logging.Logger) -> None:
    """Fail-open event CONSTRUCTION *and* write.

    ``safe_write`` guards ``sink.write`` only — but Python evaluates the
    argument expression before the call, so everything that BUILDS the event
    runs outside that guard: the vendor's scrubber, ``model_validate``,
    serialisation of an awkward value. A throw there breaks the vendor's tool
    call, which is the one thing SPEC §11.2 says capture must never do.

    Measured, not assumed, before this existed
    (`baton-internal/spikes/python_scrubber_failopen_1002/`): a vendor scrubber
    that raises turned a working tool call into ``MCPError: Internal server
    error``, and on the failing leg it REPLACED the vendor's own error. ⚠ Our
    exception did not reach the caller — FastMCP masks it — so the vendor got
    no signal pointing at Baton at all.

    Ported from ``baton-ts``'s ``emit()`` (`integrations/mcp/emit.ts`), which
    has always had this shape; the Python side never did.

    Catches ``Exception``, NOT ``BaseException`` — ``KeyboardInterrupt`` and
    ``SystemExit`` must still propagate, same rule as ``safe_write``.
    """
    event = build_or_none(build, logger)
    if event is None:
        return
    await safe_write(sink, event, logger)


def build_or_none(build: Callable[[], Event], logger: logging.Logger) -> Event | None:
    """The sync half of ``safe_emit``: build, or log and answer ``None``.

    Separate because ``Client``'s SYNC trace cannot ``await`` ``safe_emit`` and
    had grown a hand-copied duplicate of these five lines. One body, two
    callers — a guard copied by hand is a guard that drifts, which is the
    failure this whole module exists to prevent.
    """
    try:
        return build()
    except Exception:
        PROCESS_DROPS.add("encode_failed")
        logger.exception("baton: event construction failed; event dropped, tool call continues")
        return None


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
        """Flush + release resources. After ``aclose``, ``write`` raises ``SinkClosedError``."""

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
            raise SinkClosedError("StdoutSink is closed")
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
            raise SinkClosedError("FileSink is closed")
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

    def seconds_until_retry(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._opened_at + self._reset_seconds - monotonic())

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = monotonic()


#: SPEC §11.2 rule 7's default budget for a flush at shutdown, or an explicit
#: close or flush; an ``HttpSink`` may set its own.
SHUTDOWN_FLUSH_TIMEOUT_SECONDS = 3.0

#: Added to the outer limit, so an ``HttpSink``'s own warning, which counts
#: what was left, is the one shown.
_SINK_CALL_GRACE_SECONDS = 0.25

#: Calls left running at their limit, held so they are not garbage collected
#: mid-flight.
_left_running: set[asyncio.Future[Any]] = set()


#: The largest budget: ``baton-ts``'s, whose timers overflow above 2^31 - 1 ms.
_MAX_BUDGET_SECONDS = (2**31 - 1) / 1000


def _is_budget(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return 0 <= float(value) <= _MAX_BUDGET_SECONDS
    except OverflowError:
        return False


def _shutdown_budget_option(given: object) -> float:
    if _is_budget(given):
        return float(given)  # type: ignore[arg-type]
    logging.getLogger("baton").warning(
        "baton: shutdown_flush_timeout_seconds must be from 0 to %d seconds; using %.1f.",
        _MAX_BUDGET_SECONDS,
        SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
    )
    return SHUTDOWN_FLUSH_TIMEOUT_SECONDS


def shutdown_budget_seconds(sink: Sink) -> float:
    """How long a flush or close may take for ``sink``: an ``HttpSink``'s own
    budget, the longest of a ``MultiSink``'s, else the default."""
    try:
        if isinstance(sink, HttpSink):
            return sink._shutdown_flush_timeout
        if isinstance(sink, MultiSink):
            return max(
                (shutdown_budget_seconds(inner) for inner in sink._sinks),
                default=SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
            )
    except Exception:
        pass  # a subclass missing these fields, or a MultiSink that holds itself
    return SHUTDOWN_FLUSH_TIMEOUT_SECONDS


def aclose_within(sink: Sink, seconds: float) -> Awaitable[None]:
    """``sink.aclose()``, telling Baton's own close how much time is left. Any
    other close, a subclass's override included, is called as it is."""
    if type(sink).aclose is HttpSink.aclose:
        return cast(HttpSink, sink)._aclose_within(seconds)
    if type(sink).aclose is MultiSink.aclose:
        return cast(MultiSink, sink)._aclose_within(seconds)
    return sink.aclose()


async def bounded_sink_call(sink: Sink, method: Literal["flush", "aclose"]) -> None:
    """Run a sink's ``flush()`` or ``aclose()`` for the vendor (SPEC §11.2 rule
    7): never raises, and returns within the sink's shutdown budget plus a
    quarter second. Work still running then is left to finish, not cancelled."""
    log = logging.getLogger("baton")
    what = f"{method}()"
    timeout = shutdown_budget_seconds(sink) + _SINK_CALL_GRACE_SECONDS
    try:
        result = getattr(sink, method)()
        if not inspect.isawaitable(result):
            return
        task = asyncio.ensure_future(result)
    except Exception:
        log.warning("baton: %s failed", what, exc_info=True)
        return
    _left_running.add(task)
    task.add_done_callback(_left_running.discard)
    task.add_done_callback(lambda t: _log_late_failure(t, what))
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        log.warning("baton: %s took over %.2fs and was left unfinished.", what, timeout)


def _log_late_failure(task: asyncio.Future[Any], what: str) -> None:
    """Read the task's error, so asyncio does not log it as never retrieved."""
    if not task.cancelled() and task.exception() is not None:
        logging.getLogger("baton").warning("baton: %s failed", what, exc_info=task.exception())


_LOG_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _escape_for_log(text: str) -> str:
    """So text from outside cannot break or recolour a log line, or pass for an escape."""
    return "".join(
        f"\\u{{{ord(c):x}}}"
        if c == "\\" or unicodedata.category(c) in _LOG_ESCAPED_CATEGORIES
        else c
        for c in text
    )


#: SPEC §2.5's caps on one batch.
BATCH_MAX_EVENTS = 100
BATCH_MAX_BYTES = 1_048_576
#: SPEC §11.2 rule 4: how long the oldest waiting event waits for a batch to fill.
FLUSH_INTERVAL_SECONDS = 5.0

_BATCH_OPEN = b'{"events":['
_BATCH_CLOSE = b"]}"
_BATCH_PATH = "/v0/events/batch"
_SINGLE_PATH = "/v0/events"
#: A longer ``Retry-After`` is cut to this, so one header cannot stop delivery
#: while the buffer fills and drops.
_RETRY_AFTER_MAX_SECONDS = 60.0


def _encode(event: Event) -> bytes:
    return json.dumps(
        event.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _batch_body(part: list[tuple[Event, bytes]]) -> bytes:
    return _BATCH_OPEN + b",".join(body for _, body in part) + _BATCH_CLOSE


def _retry_after_seconds(headers: Any) -> float | None:
    """Seconds from a ``Retry-After`` header, or None when it is absent or an
    HTTP date, which then gets the ordinary backoff."""
    try:
        seconds = float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, _RETRY_AFTER_MAX_SECONDS)


def _text_of(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _set_once(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


async def _wait_for_thread(done: concurrent.futures.Future[None], seconds: float) -> bool:
    """Waits up to ``seconds`` for the sending thread to resolve ``done``,
    without blocking the event loop. True when it did."""
    if done.done():
        return True
    loop = asyncio.get_running_loop()
    woke: asyncio.Future[None] = loop.create_future()

    def wake(_: concurrent.futures.Future[None]) -> None:
        try:
            loop.call_soon_threadsafe(_set_once, woke)
        except RuntimeError:
            pass  # the loop closed before the thread finished

    done.add_done_callback(wake)
    await asyncio.wait({woke}, timeout=seconds)
    return woke.done()


def _proxy_for(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url)
    proxy = urllib.request.getproxies().get(parts.scheme)
    if not proxy or urllib.request.proxy_bypass(parts.hostname or ""):
        return None
    return proxy


def _pool_for(url: str) -> Any:
    urllib3 = _require_urllib3()
    import certifi

    options: dict[str, Any] = {
        "maxsize": 1,
        "retries": False,
        "cert_reqs": "CERT_REQUIRED",
        "ca_certs": certifi.where(),
    }
    proxy = _proxy_for(url)
    if proxy is not None:
        return urllib3.ProxyManager(proxy, **options)
    return urllib3.PoolManager(**options)


class _BatchSender:
    """The part of ``HttpSink`` its sending thread runs. It holds no reference
    to the sink, so a sink nobody closes can still be garbage collected."""

    REFUSAL_BODY_MAX_LEN = 500
    """Cap, in code points of the raw text before escaping, on the collector's
    refusal text copied into the vendor's log: it is a remote server's text."""
    REFUSAL_LOG_SECONDS = 60.0
    """Per status, so a collector refusing every event is not one warning per event."""
    REPORT_INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        pool: Any,
        buffer_size: int,
        request_timeout: float,
        max_retries: int,
        backoff_base: float,
        backoff_max: float,
        circuit: _CircuitBreaker,
        flush_interval: float,
    ) -> None:
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.pool = pool
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.circuit = circuit
        self.flush_interval = max(0.0, flush_interval)
        self.buffer: deque[Event] = deque(maxlen=buffer_size)
        self.recorded_at: deque[float] = deque(maxlen=buffer_size)
        # By id(): an event is alive while it is in the buffer, and leaves this
        # dict when it leaves the buffer, so an id is never reused here.
        self.encoded: dict[int, bytes] = {}
        self.overflow_warned = False
        self.one_per_request = False
        self.stopping = False
        self.retiring = False
        self.retry_at = -math.inf
        self.not_before = -math.inf
        self.flushes: list[concurrent.futures.Future[None]] = []
        self.report: tuple[ClientReportEvent, bytes, dict[str, int]] | None = None
        self.report_at = -math.inf
        self.reported: dict[str, int] = {}
        self.reports_stopped = False
        self.refusals: dict[str, tuple[float, int]] = {}
        self.thread: threading.Thread | None = None
        self.exit_hook: Callable[[], None] | None = None
        self._new_locks()

    def _new_locks(self) -> None:
        self.pid = os.getpid()
        self.cond = threading.Condition()
        self.stopped = threading.Event()
        self.refusals_lock = threading.Lock()

    def _after_fork(self) -> None:
        # The parent's thread is gone, and it may have held these locks.
        self._new_locks()
        self.thread = None
        self.flushes = []

    # ---- called from the vendor's side ---------------------------------

    def enqueue(self, event: Event) -> None:
        if self.pid != os.getpid():
            self._after_fork()
        with self.cond:
            assert self.buffer.maxlen is not None
            if len(self.buffer) >= self.buffer.maxlen:
                if not self.overflow_warned:
                    warnings.warn(
                        f"Baton HTTP sink buffer full ({self.buffer.maxlen}); "
                        "oldest events dropped. Further overflows will be silent.",
                        UserWarning,
                        stacklevel=3,
                    )
                    self.overflow_warned = True
                # After the warning: one promoted to an error drops this event
                # instead, and safe_write counts that.
                PROCESS_DROPS.add("buffer_full")
                self.encoded.pop(id(self.buffer.popleft()), None)
                self.recorded_at.popleft()
            self.buffer.append(event)
            self.recorded_at.append(monotonic())
            self.cond.notify()

    def thread_alive(self) -> bool:
        return self.pid == os.getpid() and self.thread is not None and self.thread.is_alive()

    def ensure_running(self) -> None:
        if self.pid != os.getpid():
            self._after_fork()
        if self.thread is not None and self.thread.is_alive():
            return
        with self.cond:
            if self.stopping or (self.thread is not None and self.thread.is_alive()):
                return
            self.thread = threading.Thread(target=self._run, name="baton-http-sink", daemon=True)
            self.thread.start()

    def request_flush(self) -> concurrent.futures.Future[None]:
        done: concurrent.futures.Future[None] = concurrent.futures.Future()
        with self.cond:
            if self.stopping or (not self.buffer and self.report is None):
                done.set_result(None)
            else:
                self.flushes.append(done)
                self.cond.notify()
        return done

    def stop_reports(self) -> None:
        with self.cond:
            self.reports_stopped = True
            self.report = None

    def stop(self) -> int:
        """Stops the thread after its current request; answers how many
        events it leaves unsent."""
        with self.cond:
            self.stopping = True
            left = len(self.buffer)
            self.buffer.clear()
            self.recorded_at.clear()
            self.encoded.clear()
            self.report = None
            self._finish_flushes_locked()
            self.cond.notify_all()
        self.stopped.set()
        return left

    def retire(self) -> None:
        """The sink was garbage collected: send what is left, then end the thread."""
        with self.cond:
            self.retiring = True
            self.cond.notify_all()

    # ---- the sending thread --------------------------------------------

    def _run(self) -> None:
        while True:
            self._encode_waiting()
            with self.cond:
                if self.stopping:
                    return
                now = monotonic()
                if self.retiring and not self.buffer and self.report is None:
                    if self.exit_hook is not None:
                        atexit.unregister(self.exit_hook)
                    return
                wait = self._seconds_until_due_locked(now)
                if wait is None or wait > 0:
                    self.cond.wait(wait)
                    continue
                batch = self._take_batch_locked()
            if batch:
                self._send_guarded(batch, deadline=None)

    def flush_at_exit(self, budget: float) -> None:
        """The sending thread is a daemon, and daemon threads still run while
        atexit hooks do, so it is asked to send and given the budget. With no
        thread alive (never started, or lost to a fork) the caller sends.
        Events left then are warned about, not counted (SPEC §11.2 rule 8).
        SIGKILL, or SIGTERM with no handler, skips atexit entirely."""
        self.stop_reports()
        try:
            self._flush_buffered_at_exit(budget)
        finally:
            self.log_unwarned_refusals("process exiting")

    def _flush_buffered_at_exit(self, budget: float) -> None:
        if not self.buffer:
            return
        log = logging.getLogger("baton")
        if not self.circuit.can_request():
            log.warning(
                "baton: process exiting with %d buffered event(s); "
                "circuit breaker open, skipping shutdown flush",
                len(self.buffer),
            )
            return
        try:
            if self.thread_alive():
                self.request_flush().result(timeout=budget)
            else:
                self.send_inline(monotonic() + budget)
        except TimeoutError:
            pass
        except Exception:
            log.exception(
                "baton: best-effort shutdown flush failed; %d event(s) still buffered and lost",
                len(self.buffer),
            )
            return
        if self.buffer:
            log.warning(
                "baton: process exiting without sink.aclose(); %d event(s) still buffered and lost",
                len(self.buffer),
            )

    def send_inline(self, deadline: float) -> None:
        """Sends on the calling thread until ``deadline``, for an exit with no
        sending thread alive."""
        while monotonic() < deadline:
            self._encode_waiting()
            with self.cond:
                if self.stopping or not self.circuit.can_request() or self.not_before > monotonic():
                    return
                batch = self._take_batch_locked()
            if not batch or not self._send_guarded(batch, deadline):
                return

    def _send_guarded(self, batch: list[tuple[Event, bytes]], deadline: float | None) -> bool:
        try:
            return self._send(batch, deadline)
        except Exception:
            # Not the event's fault (a bad URL or key, say), so it is kept.
            logging.getLogger("baton").warning("baton: sending events failed", exc_info=True)
            self._failed()
            return False

    def _seconds_until_due_locked(self, now: float) -> float | None:
        """0 to send now, else how long to wait; None waits for a write."""
        if not self.buffer and self.report is None:
            self._finish_flushes_locked()
            return None
        if not self.circuit.can_request():
            self._finish_flushes_locked()
            return self.circuit.seconds_until_retry()
        if self.not_before > now:
            self._finish_flushes_locked()
            return self.not_before - now
        if self.flushes or self.retiring:
            return 0.0
        if self.retry_at > now:
            return self.retry_at - now
        if len(self.buffer) >= BATCH_MAX_EVENTS or self._over_byte_cap_locked():
            return 0.0
        oldest = self.recorded_at[0] if self.recorded_at else math.inf
        if self.report is not None:
            oldest = min(oldest, self.report_at)
        return max(0.0, oldest + self.flush_interval - now)

    def _over_byte_cap_locked(self) -> bool:
        size = len(_BATCH_OPEN) + len(_BATCH_CLOSE) - 1
        for event in itertools.islice(self.buffer, BATCH_MAX_EVENTS):
            body = self.encoded.get(id(event))
            if body is None:
                return False
            size += len(body) + 1
            if size > BATCH_MAX_BYTES:
                return True
        return False

    def _finish_flushes_locked(self) -> None:
        for done in self.flushes:
            if not done.done():
                done.set_result(None)
        self.flushes = []

    def _encode_waiting(self) -> None:
        with self.cond:
            todo = [
                e
                for e in itertools.islice(self.buffer, BATCH_MAX_EVENTS)
                if id(e) not in self.encoded
            ]
        if not todo:
            return
        bodies: list[tuple[Event, bytes | None]] = []
        failed: list[tuple[Event, Exception]] = []
        for event in todo:
            try:
                bodies.append((event, _encode(event)))
            except Exception as exc:
                bodies.append((event, None))
                failed.append((event, exc))
        dropped: list[tuple[Event, Exception]] = []
        with self.cond:
            for event, body in bodies:
                index = next((i for i, e in enumerate(self.buffer) if e is event), None)
                if index is None:
                    continue
                if body is not None:
                    self.encoded[id(event)] = body
                    continue
                del self.buffer[index]
                del self.recorded_at[index]
                PROCESS_DROPS.add("encode_failed")
                dropped.extend((e, x) for e, x in failed if e is event)
        for event, error in dropped:
            # Only the exception TYPE: its message can quote the event's own text.
            logging.getLogger("baton").warning(
                "baton: a %s event could not be encoded and has been dropped (%s) "
                "— the event is gone; the events after it are still sent.",
                event.event_type,
                type(error).__name__,
            )

    def _take_batch_locked(self) -> list[tuple[Event, bytes]]:
        """The next batch from the front of the buffer, within SPEC §2.5's caps
        and with one ``tenant_id`` and ``vendor_id``, which §2.5 requires."""
        batch: list[tuple[Event, bytes]] = []
        size = len(_BATCH_OPEN) + len(_BATCH_CLOSE) - 1
        owner: tuple[str, str] | None = None

        def fits(event: Event, body: bytes) -> bool:
            if not batch:
                return True
            return (event.tenant_id, event.vendor_id) == owner and (
                size + len(body) + 1 <= BATCH_MAX_BYTES
            )

        for event in itertools.islice(self.buffer, BATCH_MAX_EVENTS):
            body = self.encoded.get(id(event))
            if body is None or not fits(event, body):
                break
            owner = (event.tenant_id, event.vendor_id)
            size += len(body) + 1
            batch.append((event, body))
        if self.report is not None and len(batch) < BATCH_MAX_EVENTS:
            report, body, _ = self.report
            if fits(report, body):
                batch.append((report, body))
        return batch

    def _send(self, batch: list[tuple[Event, bytes]], deadline: float | None) -> bool:
        """True when every event in ``batch`` was delivered or lost; False when
        some are kept for a later try."""
        parts = [batch]
        while parts:
            part = parts.pop(0)
            if self.one_per_request:
                if not self._send_one_per_request(part, deadline):
                    return False
                continue
            answer = self._post(_BATCH_PATH, _batch_body(part), deadline)
            if answer is None:
                return False
            status, data = answer
            if status in (404, 405):
                self.one_per_request = True
                logging.getLogger("baton").info(
                    "baton: the collector has no batch endpoint; sending one event per request."
                )
                parts.insert(0, part)
            elif status == 413 and len(part) > 1:
                half = len(part) // 2
                parts[0:0] = [part[:half], part[half:]]
            elif 200 <= status < 300:
                self._settle(_per_event_results(part, data), "200", report_refusal_stops=True)
            else:
                text = _text_of(data)
                self._settle(
                    [(e, text) for e, _ in part],
                    str(status),
                    report_refusal_stops=status in (400, 422),
                )
        return True

    def _send_one_per_request(
        self, part: list[tuple[Event, bytes]], deadline: float | None
    ) -> bool:
        for event, body in part:
            answer = self._post(_SINGLE_PATH, body, deadline)
            if answer is None:
                return False
            status, data = answer
            refusal = None if 200 <= status < 300 else _text_of(data)
            self._settle([(event, refusal)], str(status), report_refusal_stops=status in (400, 422))
        return True

    def _post(self, path: str, body: bytes, deadline: float | None) -> tuple[int, bytes] | None:
        """The collector's final answer, or None when the events are kept: a
        5xx, 429 or network failure after the retries, or a ``Retry-After``."""
        urllib3 = _require_urllib3()
        for attempt in range(self.max_retries + 1):
            timeout = self.request_timeout
            if deadline is not None:
                timeout = min(timeout, deadline - monotonic())
                if timeout <= 0:
                    return None
            try:
                response = self.pool.urlopen(
                    "POST",
                    self.base_url + path,
                    body=body,
                    headers=self.headers,
                    timeout=urllib3.Timeout(total=timeout),
                    retries=False,
                    redirect=False,
                )
            except urllib3.exceptions.HTTPError:
                pass
            else:
                status = response.status
                if status == 429:
                    wait = _retry_after_seconds(response.headers)
                    if wait is not None:
                        # The endpoint answered, so the circuit is not told.
                        with self.cond:
                            self.not_before = monotonic() + wait
                            self._finish_flushes_locked()
                        return None
                elif 200 <= status < 300 or 400 <= status < 500:
                    # A 4xx proves the endpoint is alive: opening the circuit
                    # would stop the good events too.
                    self.circuit.record_success()
                    return status, response.data
            if attempt < self.max_retries:
                backoff = min(self.backoff_base * (2**attempt), self.backoff_max)
                if deadline is not None:
                    backoff = min(backoff, max(0.0, deadline - monotonic()))
                if self.stopped.wait(backoff):
                    return None
        self._failed()
        return None

    def _failed(self) -> None:
        self.circuit.record_failure()
        with self.cond:
            self.retry_at = monotonic() + self.flush_interval
            self._finish_flushes_locked()

    def _settle(
        self, outcomes: list[tuple[Event, str | None]], label: str, *, report_refusal_stops: bool
    ) -> None:
        """Takes answered events out of the buffer. An outcome's text is None
        for a delivered event, else what the collector said refusing it."""
        refused: list[str] = []
        delivered: Event | None = None
        with self.cond:
            if self.stopping:
                return
            for event, refusal in outcomes:
                if self.report is not None and self.report[0] is event:
                    if refusal is None:
                        self.reported = self.report[2]
                    elif report_refusal_stops:
                        self.reports_stopped = True
                    self.report = None
                    continue
                # A full buffer may already have pushed it out, and counted it.
                if not self.buffer or self.buffer[0] is not event:
                    continue
                self.buffer.popleft()
                self.recorded_at.popleft()
                self.encoded.pop(id(event), None)
                if refusal is None:
                    delivered = event
                else:
                    refused.append(refusal)
            if refused:
                PROCESS_DROPS.add("refused", len(refused))
            if delivered is not None:
                self._maybe_report_locked(delivered)
        if refused:
            self._log_refusal(label, refused[0], len(refused))
        if delivered is not None:
            PROCESS_DROPS.log_recovery()

    def _maybe_report_locked(self, delivered: Event) -> None:
        """Queues a report of this process's loss totals (SPEC §11.4.7) for the
        next batch. It is kept outside the buffer, so it never pushes out an
        event and is never counted as one."""
        now = monotonic()
        if (
            self.reports_stopped
            or self.report is not None
            or now - self.report_at < self.REPORT_INTERVAL_SECONDS
        ):
            return
        totals: dict[str, int] = {reason: n for reason, n in PROCESS_DROPS.total().items()}
        if totals == self.reported:
            return
        try:
            report = ClientReportEvent(
                tenant_id=delivered.tenant_id,
                vendor_id=delivered.vendor_id,
                consent_token=delivered.consent_token,
                session_id=str(PROCESS_DROPS.instance_id),
                sequence_number=PROCESS_SEQUENCE.next(),
                captured_at=datetime.now(UTC),
                payload=ClientReportPayload(instance_id=PROCESS_DROPS.instance_id, dropped=totals),
            )
            body = _encode(report)
        except Exception:
            return
        self.report_at = now
        self.report = (report, body, totals)

    def _log_refusal(self, label: str, detail: str, count: int) -> None:
        """A refused event is otherwise dropped silently, which looks the same
        from the vendor's process as an accepted one. The collector's text is
        logged because it names the field at fault."""
        now = monotonic()
        with self.refusals_lock:
            logged_at, unlogged = self.refusals.get(label, (-math.inf, 0))
            if now - logged_at < self.REFUSAL_LOG_SECONDS:
                self.refusals[label] = (logged_at, unlogged + count)
                return
            self.refusals[label] = (now, 0)
        logging.getLogger("baton").warning(
            "baton: collector REFUSED %s (HTTP %s): %s — the event is gone; "
            "nothing retries a refusal.%s",
            "an event and it has been dropped"
            if count == 1
            else f"{count} events and they have been dropped",
            label,
            _escape_for_log(detail[: self.REFUSAL_BODY_MAX_LEN]),
            f" {unlogged} more refused since the last such warning." if unlogged else "",
        )

    def log_unwarned_refusals(self, when: str) -> None:
        with self.refusals_lock:
            unlogged = sum(n for _, n in self.refusals.values())
            self.refusals = {label: (at, 0) for label, (at, _) in self.refusals.items()}
        if unlogged:
            logging.getLogger("baton").warning(
                "baton: %s; %d more event(s) refused since the last such warning.",
                when,
                unlogged,
            )


def _per_event_results(
    part: list[tuple[Event, bytes]], data: bytes
) -> list[tuple[Event, str | None]]:
    """SPEC §2.5's results, matched by ``event_id``. An event the answer does
    not name was taken: the collector answered 2xx."""
    refusals: dict[str, str] = {}
    try:
        for result in json.loads(data)["results"]:
            if result.get("status") == "refused":
                error = result.get("error") or {}
                refusals[str(result["event_id"])] = (
                    f"{error.get('code', 'refused')}: {error.get('message', '')}"
                )
    except Exception:
        pass
    return [(event, refusals.get(str(event.event_id))) for event, _ in part]


class HttpSink(Sink):
    """POSTs events to a collector in batches (SPEC §2.5): ``POST
    {url}/v0/events/batch`` with ``Authorization: Bearer {api_key}`` and a body
    ``{"events": [...]}``.

    ``write()`` only queues the event. A daemon thread owned by the sink sends:
    when 100 events wait, when the next would take the body over 1 MiB, or
    ``flush_interval_seconds`` after the oldest waiting event was written. One
    request at a time. The queue is bounded and drops the oldest event when
    full, with one ``UserWarning``; failed sends retry with backoff, and a
    circuit breaker stops sending to an endpoint that keeps failing.
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
        shutdown_flush_timeout_seconds: float = SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
        flush_interval_seconds: float = FLUSH_INTERVAL_SECONDS,
        _http_client: urllib3.PoolManager | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._circuit = _CircuitBreaker(circuit_breaker_threshold, circuit_breaker_reset_seconds)
        self._shutdown_flush_timeout = _shutdown_budget_option(shutdown_flush_timeout_seconds)
        self._owns_client = _http_client is None
        self._http_client = _pool_for(self._url) if _http_client is None else _http_client
        self._sender = _BatchSender(
            base_url=self._url,
            api_key=api_key,
            pool=self._http_client,
            buffer_size=buffer_size,
            request_timeout=request_timeout_seconds,
            max_retries=max_retries,
            backoff_base=backoff_base_seconds,
            backoff_max=backoff_max_seconds,
            circuit=self._circuit,
            flush_interval=flush_interval_seconds,
        )
        finalizer = weakref.finalize(self, self._sender.retire)
        finalizer.atexit = False  # type: ignore[misc]
        # The flush or close wait that ran out of budget, until the thread ends it.
        self._stalled: concurrent.futures.Future[None] | None = None
        self._skip_warned = False
        self._closing: asyncio.Future[None] | None = None
        self._closed = False
        self._atexit_registered = False
        self._atexit_callback: Callable[[], None] | None = None

    @property
    def url(self) -> str:
        """Base Console URL (no trailing slash)."""
        return self._url

    @property
    def api_key(self) -> str:
        """Bearer token used to authenticate with the Console."""
        return self._api_key

    @property
    def _buffer(self) -> deque[Event]:
        return self._sender.buffer

    async def write(self, event: Event) -> None:
        if self._closed:
            raise SinkClosedError("HttpSink is closed")
        self._sender.enqueue(event)
        self._sender.ensure_running()
        self._ensure_atexit_registered()

    async def flush(self) -> None:
        if self._closed:
            return
        await self._wait_for_sender("flush()", self._shutdown_flush_timeout)

    async def aclose(self) -> None:
        """A second call waits for the first (SPEC §11.2 rule 7)."""
        await self._aclose_within(self._shutdown_flush_timeout)

    async def _aclose_within(self, seconds: float) -> None:
        # A close cancelled from outside (loop teardown) is started again.
        if self._closing is None or self._closing.cancelled():
            seconds = min(seconds, self._shutdown_flush_timeout)
            self._closing = asyncio.ensure_future(self._close(seconds))
        await asyncio.shield(self._closing)

    async def _close(self, seconds: float) -> None:
        log = logging.getLogger("baton")
        self._sender.stop_reports()
        await self._wait_for_sender("aclose()", seconds)
        self._closed = True
        unsent = self._sender.stop()
        if unsent:
            PROCESS_DROPS.add("unsent_at_close", unsent)
            log.warning("baton: the sink closed with %d event(s) unsent.", unsent)
        self._sender.log_unwarned_refusals("the sink closed")
        if self._atexit_callback is not None:
            atexit.unregister(self._atexit_callback)
            self._atexit_registered = False
            self._atexit_callback = None
        if self._owns_client:
            try:
                self._http_client.clear()
            except Exception:
                log.warning("baton: closing the HTTP connections failed", exc_info=True)

    async def _wait_for_sender(self, what: Literal["flush()", "aclose()"], seconds: float) -> None:
        """SPEC §11.2 rule 7: returns within ``seconds`` and never raises.
        While an earlier wait is still stuck, a flush returns at once and a
        close gives it ``seconds``. The thread goes on sending after a flush
        gives up; ``aclose()`` stops it."""
        log = logging.getLogger("baton")
        stalled = self._stalled
        if stalled is not None and not stalled.done():
            if what == "aclose()":
                await _wait_for_thread(stalled, seconds)
                return
            if not self._skip_warned:
                self._skip_warned = True
                log.warning(
                    "baton: flush() skipped: an earlier drain is still stuck; "
                    "%d event(s) wait to be sent.",
                    len(self._buffer),
                )
            return
        if self._buffer:
            self._sender.ensure_running()
        done = self._sender.request_flush()
        if await _wait_for_thread(done, seconds):
            return
        self._stalled = done
        self._skip_warned = False
        if what == "flush()":
            log.warning(
                "baton: flush() took over %.1fs; it goes on in the background.",
                self._shutdown_flush_timeout,
            )

    def _enqueue_for_test(self, event: Event) -> None:
        """Queues without starting the sending thread."""
        self._sender.enqueue(event)

    def _ensure_atexit_registered(self) -> None:
        # Registered on first write, and through a weakref so an unclosed sink
        # can still be garbage collected; its sender then gets the exit flush.
        # Unregistered by aclose().
        if not self._atexit_registered:
            weak_self = weakref.ref(self)
            sender = self._sender
            budget = self._shutdown_flush_timeout

            def _flush_if_alive() -> None:
                sink = weak_self()
                if sink is not None:
                    sink._atexit_flush()
                else:
                    sender.flush_at_exit(budget)

            atexit.register(_flush_if_alive)
            sender.exit_hook = _flush_if_alive
            self._atexit_callback = _flush_if_alive
            self._atexit_registered = True

    def _atexit_flush(self) -> None:
        """Best-effort send when the process exits without ``aclose()``."""
        self._sender.stop_reports()
        if self._closed:
            return
        self._sender.flush_at_exit(self._shutdown_flush_timeout)


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
        await self._aclose_within(math.inf)

    async def _aclose_within(self, seconds: float) -> None:
        await self._fan_out(lambda s: aclose_within(s, seconds))

    async def _fan_out(self, op: object) -> None:
        results = await asyncio.gather(
            *(op(s) for s in self._sinks),  # type: ignore[operator]
            return_exceptions=True,
        )
        # Narrow to Exception (not BaseException) — never aggregate
        # CancelledError / KeyboardInterrupt into a sink-failure group.
        errors = [r for r in results if isinstance(r, Exception)]
        if any(r is None for r in results):
            # Another sink took the event, so it was not lost.
            errors = [e for e in errors if not isinstance(e, SinkClosedError)]
        if errors:
            if len(errors) == 1 or all(isinstance(e, SinkClosedError) for e in errors):
                raise errors[0]
            raise ExceptionGroup("MultiSink fan-out failures", errors)


__all__ = [
    "FileSink",
    "HttpSink",
    "MultiSink",
    "Sink",
    "SinkClosedError",
    "StdoutSink",
]
