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
import atexit
import inspect
import json
import logging
import math
import sys
import threading
import unicodedata
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
    import httpx


def _require_httpx() -> Any:
    """Import httpx or raise a pointed install hint.

    httpx is a core dependency, imported here rather than at module scope so
    ``import baton`` does not load it when no ``HttpSink`` is built. httpx (not
    urllib) because the SDK emits from inside the event loop, where a blocking
    client would stall it.
    """
    try:
        import httpx
    except ImportError as e:  # pragma: no cover - a broken install
        raise ImportError(
            "HttpSink needs httpx, which baton-sdk depends on, and this "
            "environment does not have it. Reinstall with: pip install baton-sdk"
        ) from e
    return httpx


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
        shutdown_flush_timeout_seconds: float = SHUTDOWN_FLUSH_TIMEOUT_SECONDS,
        _http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._buffer: deque[Event] = deque(maxlen=buffer_size)
        self._overflow_warned = False
        self._refusals: dict[int, tuple[float, int]] = {}
        # The atexit flush reads this on the main thread while a sync Client's
        # loop thread may still be writing it.
        self._refusals_lock = threading.Lock()
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._circuit = _CircuitBreaker(circuit_breaker_threshold, circuit_breaker_reset_seconds)
        self._shutdown_flush_timeout = _shutdown_budget_option(shutdown_flush_timeout_seconds)
        self._owns_client = _http_client is None
        if _http_client is None:
            httpx = _require_httpx()
            _http_client = httpx.AsyncClient(timeout=httpx.Timeout(request_timeout_seconds))
        self._http_client = _http_client
        self._flush_lock = asyncio.Lock()
        self._drain_task: asyncio.Task[None] | None = None
        # The drain a flush() or aclose() gave up waiting on, until it ends.
        self._stalled_drain: asyncio.Future[None] | None = None
        self._skip_warned = False
        self._open_drains: set[asyncio.Future[None]] = set()
        self._closing: asyncio.Future[None] | None = None
        self._closed = False
        self._atexit_registered = False
        self._atexit_callback: Callable[[], None] | None = None
        self._reported: dict[str, int] = {}
        self._last_report_at = -math.inf
        self._reports_stopped = False
        self._report_task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        """Base Console URL (no trailing slash)."""
        return self._url

    @property
    def api_key(self) -> str:
        """Bearer token used to authenticate with the Console."""
        return self._api_key

    async def write(self, event: Event) -> None:
        if self._closed:
            raise SinkClosedError("HttpSink is closed")
        self._enqueue(event)
        self._ensure_drain_running()
        self._ensure_atexit_registered()

    async def flush(self) -> None:
        if self._closed:
            return
        await self._drain_within_budget("flush()", self._shutdown_flush_timeout)

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
        self._reports_stopped = True
        if self._report_task is not None:
            self._report_task.cancel()
        await self._drain_within_budget("aclose()", seconds)
        self._closed = True
        # Nothing Baton started may keep running after the vendor closed it.
        for task in (*self._open_drains, self._drain_task):
            if task is not None and not task.done():
                task.cancel()
        if self._buffer:
            PROCESS_DROPS.add("unsent_at_close", len(self._buffer))
            log.warning("baton: the sink closed with %d event(s) unsent.", len(self._buffer))
        self._log_unwarned_refusals("the sink closed")
        if self._atexit_callback is not None:
            atexit.unregister(self._atexit_callback)
            self._atexit_registered = False
            self._atexit_callback = None
        if self._owns_client:
            try:
                await self._http_client.aclose()
            except Exception:
                log.warning("baton: closing the HTTP client failed", exc_info=True)

    async def _drain_within_budget(
        self, what: Literal["flush()", "aclose()"], seconds: float
    ) -> None:
        """SPEC §11.2 rule 7: returns within ``seconds`` and never raises.
        While an earlier wait's drain is still stuck, a flush returns at once
        and a close gives it ``seconds``. The drain is not cancelled here, so
        after ``flush()`` it goes on sending; ``aclose()`` stops it."""
        log = logging.getLogger("baton")
        if self._stalled_drain is not None:
            if what == "aclose()":
                await asyncio.wait({self._stalled_drain}, timeout=seconds)
                return
            if not self._skip_warned:
                self._skip_warned = True
                log.warning(
                    "baton: flush() skipped: an earlier drain is still stuck; "
                    "%d event(s) wait to be sent.",
                    len(self._buffer),
                )
            return
        drain = asyncio.ensure_future(self._drain_now())
        self._open_drains.add(drain)
        drain.add_done_callback(self._open_drains.discard)
        drain.add_done_callback(lambda t: _log_late_failure(t, what))
        done, _ = await asyncio.wait({drain}, timeout=seconds)
        if done:
            return
        self._stalled_drain = drain

        def _clear(_: asyncio.Future[None]) -> None:
            if self._stalled_drain is drain:
                self._stalled_drain = None
                self._skip_warned = False

        drain.add_done_callback(_clear)
        if what == "flush()":
            log.warning(
                "baton: flush() took over %.1fs; it goes on in the background.",
                self._shutdown_flush_timeout,
            )

    async def _flush_at_exit(self) -> None:
        # At exit a stuck drain gets the budget too, rather than the skip a
        # later flush() takes.
        stalled = self._stalled_drain
        if stalled is not None:
            await asyncio.wait({stalled}, timeout=self._shutdown_flush_timeout)
        else:
            await self.flush()

    async def _drain_now(self) -> None:
        if self._drain_task is not None and not self._drain_task.done():
            await self._drain_task
        async with self._flush_lock:
            await self._drain_locked()

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
            # After the warning: one promoted to an error drops this event
            # instead, and safe_write counts that.
            PROCESS_DROPS.add("buffer_full")
        self._buffer.append(event)

    def _dequeue(self, event: Event) -> None:
        # A full buffer may already have pushed ``event`` out while it was sent.
        if self._buffer and self._buffer[0] is event:
            self._buffer.popleft()

    # Test hook — see test_sinks.py for the overflow-semantics tests.
    def _enqueue_for_test(self, event: Event) -> None:
        self._enqueue(event)

    def _ensure_drain_running(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._background_drain())

    def _ensure_atexit_registered(self) -> None:
        # Registered on first write (not at import) so a constructed-but-unused
        # sink holds no interpreter-lifetime reference. Weakref-wrapped so a
        # caller that writes and never calls aclose() can still be garbage
        # collected normally once nothing else references the sink — the
        # hook becomes a no-op rather than pinning the sink (and its buffer/
        # connection pool) alive for the process's whole lifetime just by
        # having been written to once. Unregistered in aclose(): an explicit
        # clean shutdown means there's nothing left for the hook to do.
        if not self._atexit_registered:
            weak_self = weakref.ref(self)

            def _flush_if_alive() -> None:
                sink = weak_self()
                if sink is not None:
                    sink._atexit_flush()

            atexit.register(_flush_if_alive)
            self._atexit_callback = _flush_if_alive
            self._atexit_registered = True

    def _events_url(self) -> str:
        return f"{self._url}/v0/events"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    #: Cap, in code points of the raw text before escaping, on the collector's
    #: refusal text copied into the vendor's log: it is a remote server's text.
    _REFUSAL_BODY_MAX_LEN = 500
    #: Per status, so a collector refusing every event is not one warning per event.
    _REFUSAL_LOG_SECONDS = 60.0

    @staticmethod
    def _classify_status(status: int) -> str:
        if 200 <= status < 300:
            return "success"
        if 400 <= status < 500 and status != 429:
            return "permanent_failure"
        return "transient_failure"

    def _log_refusal(self, response: Any) -> None:
        """A refused event is otherwise dropped silently, which looks the same
        from the vendor's process as an accepted one. The body is logged
        because it names the field at fault. The circuit breaker is not told:
        a 4xx proves the endpoint is alive, and opening the circuit would stop
        the good events too.
        """
        PROCESS_DROPS.add("refused")
        status = response.status_code
        now = monotonic()
        with self._refusals_lock:
            logged_at, unlogged = self._refusals.get(status, (-math.inf, 0))
            if now - logged_at < self._REFUSAL_LOG_SECONDS:
                self._refusals[status] = (logged_at, unlogged + 1)
                return
            self._refusals[status] = (now, 0)
        try:
            detail = _escape_for_log(response.text[: self._REFUSAL_BODY_MAX_LEN])
        except Exception:  # pragma: no cover — a body that cannot be decoded
            detail = "<unreadable>"
        logging.getLogger("baton").warning(
            "baton: collector REFUSED an event and it has been dropped "
            "(HTTP %s): %s — the event is gone; nothing retries a 4xx.%s",
            status,
            detail,
            f" {unlogged} more refused since the last such warning." if unlogged else "",
        )

    def _log_unwarned_refusals(self, when: str) -> None:
        with self._refusals_lock:
            unlogged = sum(n for _, n in self._refusals.values())
            self._refusals = {status: (at, 0) for status, (at, _) in self._refusals.items()}
        if unlogged:
            logging.getLogger("baton").warning(
                "baton: %s; %d more event(s) refused since the last such warning.",
                when,
                unlogged,
            )

    def _build_request_or_none(self, client: Any, event: Event, **kwargs: Any) -> Any:
        """None when the event cannot be made into a request; it is logged."""
        # Encoding fails the same way on every attempt, so the caller drops the
        # event instead of retrying. Only the exception TYPE is logged: its
        # message can quote the event's own text.
        import httpx

        # Outside the try: a malformed URL or key is the sink's fault, not the event's.
        url = httpx.URL(self._events_url())
        headers = httpx.Headers(self._auth_headers())
        try:
            return client.build_request(
                "POST",
                url,
                json=event.model_dump(mode="json"),
                headers=headers,
                **kwargs,
            )
        except Exception as exc:
            PROCESS_DROPS.add("encode_failed")
            logging.getLogger("baton").warning(
                "baton: a %s event could not be encoded and has been dropped (%s) "
                "— the event is gone; the events after it are still sent.",
                event.event_type,
                type(exc).__name__,
            )
            return None

    def _atexit_flush(self) -> None:
        """Best-effort flush when the process exits without an explicit
        ``aclose()`` — a forgotten shutdown call, or an unhandled exception
        unwinding past ``asyncio.run()``.

        Two distinct cases, disambiguated by whether the event loop that owns
        ``_drain_task`` is still alive:

        - **Dead loop** — the common case: ``asyncio.run()`` cancels
          ``_background_drain`` and closes its loop before returning
          (verified: a clean ``CancelledError``, not a crash — asyncio tasks
          don't hit the ``threading``-only interpreter-abort risk a raw
          background thread would). The original ``AsyncClient``/lock/task
          are all unusable now, but cancellation only drops an event from
          ``self._buffer`` on success or permanent failure, so anything
          in-flight or not yet attempted survives — drained here with a
          fresh sync client, one attempt per event (no retry; the process is
          exiting), the remaining budget recomputed before every request so
          total wall time can't exceed ``shutdown_flush_timeout_seconds``
          regardless of how many events are buffered.
        - **Live loop** — the sync ``Client``'s background-thread bridge
          (``_SyncBridge``) runs its own loop via ``run_forever()`` on a
          separate thread until ``Client.close()`` stops it; if the vendor
          never calls ``close()``, that loop is still alive and potentially
          mid-drain when this fires on the main thread. Touching
          ``self._buffer``/``self._http_client`` directly here would race
          with it (an ``asyncio.Lock`` only serializes coroutines on one
          loop, not calls from a second thread) — instead, hand the real
          ``flush()`` to that loop via ``run_coroutine_threadsafe`` (the
          thread-safe way to submit work to a loop running elsewhere) and
          block on the result under the same bounded deadline. `Client`
          itself has no shutdown hook of its own today — this is the only
          safety net for a never-closed sync `Client`, and if the bridge
          thread is killed before its flush task finishes when the
          interpreter actually tears down, that's a pre-existing gap in
          `_SyncBridge`, not something this fix introduces or can close.

        Real residual gap, not fixable in-process either way: SIGKILL, or
        SIGTERM with no vendor-installed handler, terminate before Python's
        normal shutdown sequence — and therefore atexit — ever runs. A
        vendor needing that guarantee must call ``aclose()``/``close()`` from
        their own signal handler.
        """
        self._reports_stopped = True
        if self._closed:
            return
        try:
            self._flush_buffered_at_exit()
        finally:
            self._log_unwarned_refusals("process exiting")

    def _flush_buffered_at_exit(self) -> None:
        if not self._buffer:
            return
        log = logging.getLogger("baton")

        loop = self._drain_task.get_loop() if self._drain_task is not None else None
        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self._flush_at_exit(), loop)
                future.result(timeout=self._shutdown_flush_timeout + _SINK_CALL_GRACE_SECONDS)
            except TimeoutError:
                log.warning(
                    "baton: process exiting; cross-thread shutdown flush did not "
                    "complete within %.1fs with %d event(s) unsent (background thread "
                    "may still be draining when the process actually exits)",
                    self._shutdown_flush_timeout,
                    len(self._buffer),
                )
                return
            except Exception:
                log.exception("baton: cross-thread shutdown flush failed")
            if self._buffer:
                log.warning(
                    "baton: process exiting without sink.aclose(); %d event(s) still "
                    "buffered and lost",
                    len(self._buffer),
                )
            return

        if not self._circuit.can_request():
            log.warning(
                "baton: process exiting with %d buffered event(s); "
                "circuit breaker open, skipping shutdown flush",
                len(self._buffer),
            )
            return
        try:
            import httpx

            deadline = monotonic() + self._shutdown_flush_timeout
            sent = 0
            dropped = 0
            with httpx.Client() as client:
                while self._buffer:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    request = self._build_request_or_none(
                        client, self._buffer[0], timeout=remaining
                    )
                    if request is None:
                        self._buffer.popleft()
                        dropped += 1
                        continue
                    try:
                        response = client.send(request)
                    except httpx.HTTPError:
                        break  # transient — process is exiting, don't burn the deadline retrying
                    outcome = self._classify_status(response.status_code)
                    if outcome == "success":
                        self._buffer.popleft()
                        sent += 1
                    elif outcome == "permanent_failure":
                        self._log_refusal(response)
                        self._buffer.popleft()  # unrecoverable, give up
                        dropped += 1
                    else:
                        break  # transient (429/5xx)
            remaining_count = len(self._buffer)
            if remaining_count or dropped:
                log.warning(
                    "baton: process exiting without sink.aclose(); "
                    "%d event(s) flushed, %d dropped (refused or unencodable), "
                    "%d still buffered and lost",
                    sent,
                    dropped,
                    remaining_count,
                )
        except Exception:
            log.exception(
                "baton: best-effort shutdown flush failed; %d event(s) still buffered and lost",
                len(self._buffer),
            )

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
                self._dequeue(event)
                self._circuit.record_success()
                PROCESS_DROPS.log_recovery()
                self._maybe_report(event)
            elif outcome == "permanent_failure":
                self._dequeue(event)
                self._circuit.record_success()
            elif outcome == "unencodable":
                # Nothing was sent, so the circuit learns nothing about the endpoint.
                self._dequeue(event)
            else:
                self._circuit.record_failure()
                return

    _REPORT_INTERVAL_SECONDS = 60.0

    def _maybe_report(self, delivered: Event) -> None:
        """Start a report of this process's loss totals (SPEC §11.4.7) beside
        the drain and outside the buffer, so it never delays an event and is
        never counted as one."""
        now = monotonic()
        if (
            self._reports_stopped
            or (self._report_task is not None and not self._report_task.done())
            or now - self._last_report_at < self._REPORT_INTERVAL_SECONDS
        ):
            return
        totals: dict[str, int] = {reason: n for reason, n in PROCESS_DROPS.total().items()}
        if totals == self._reported:
            return
        self._last_report_at = now
        self._report_task = asyncio.ensure_future(self._send_report(delivered, totals))

    async def _send_report(self, delivered: Event, totals: dict[str, int]) -> None:
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
            response = await self._http_client.post(
                self._events_url(),
                json=report.model_dump(mode="json"),
                headers=self._auth_headers(),
            )
        except Exception:
            return
        if response.status_code in (400, 422):
            self._reports_stopped = True
        elif 200 <= response.status_code < 300:
            self._reported = totals

    async def _send_with_retry(self, event: Event) -> str:
        import httpx  # importable — constructing this sink already imported it

        request = self._build_request_or_none(self._http_client, event)
        if request is None:
            return "unencodable"

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http_client.send(request)
                outcome = self._classify_status(response.status_code)
                if outcome == "permanent_failure":
                    # Logged HERE, which is the only scope holding the
                    # response — the drain loop above sees a bare string.
                    self._log_refusal(response)
                if outcome != "transient_failure":
                    return outcome
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
