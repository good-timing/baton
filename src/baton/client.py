"""Library API — ``Client`` and ``AsyncClient`` for Skill-instrumented agent code.

For vendors whose customers reach the vendor API via agent-generated code
(Skills pattern, not MCP). The agent's generated code imports this client,
wraps each tool call with ``client.trace(...)``, and optionally annotates
friction signals reactively with ``client.annotate(...)``.

Both ``Client`` (sync) and ``AsyncClient`` (async) share the same surface and
emit the same SPEC §11.4 event envelopes as the MCP integration — feeds the
same Console ingest, same correlation rules, same SignalPayload.

Sync usage (drives the async ``Sink`` via a background daemon thread
running a persistent event loop; standard pattern for sync-over-async SDKs
like Sentry):

    from baton import Client, SignalType
    from baton.sinks import HttpSink

    client = Client(
        vendor_id="acme",
        consent_token="...",
        sink=HttpSink("https://acme.console.example.com", api_key="bk_live_..."),
    )
    try:
        with client.trace(
            tool_name="chat.completions.create",
            intent="answer the user's question about X",
            expected_outcome="a complete answer based on retrieved context",
        ) as trace:
            trace.with_params({"model": "...", "messages": [...]})
            result = vendor_client.chat.completions.create(...)
            trace.observed(result)
        # ...later, if friction:
        client.annotate(
            signal_type=SignalType.DEAD_END,
            suggested_improvement="model doesn't expose latency metadata",
        )
    finally:
        client.close()

Async usage (no thread bridge; directly drives the async ``Sink``):

    from baton import AsyncClient, SignalType
    from baton.sinks import HttpSink

    client = AsyncClient(
        vendor_id="acme",
        consent_token="...",
        sink=HttpSink("https://acme.console.example.com", api_key="bk_live_..."),
    )
    try:
        async with client.trace(...) as trace:
            trace.with_params({...})
            result = await vendor_client.chat.completions.create(...)
            trace.observed(result)
        await client.annotate(signal_type=SignalType.DEAD_END, ...)
    finally:
        await client.aclose()

Config loading: explicit kwargs win; env-var fallback supported for
``vendor_id`` (``BATON_VENDOR_ID``) and ``consent_token``
(``BATON_CONSENT_TOKEN``). ``sink`` is explicit only — construct your sink
from env vars at the call site if you want that behavior. ``consent_token``
may be overridden per-trace.
"""

from __future__ import annotations

import asyncio
import os
import threading
import traceback
from collections.abc import Coroutine
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from types import TracebackType
from typing import Any, Self, TypeVar
from uuid import uuid7

from baton.events import (
    AnnotationEvent,
    AnnotationPayload,
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallErrorEvent,
    ToolCallErrorPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from baton.scrub import identity_scrub
from baton.sinks import Sink

T = TypeVar("T")

__all__ = [
    "AsyncClient",
    "AsyncTrace",
    "Client",
    "SignalType",
    "Trace",
]


# Sentinel for "observed() not called" vs "observed(result=None) explicitly".
# Defined at module top so Trace and AsyncTrace can use it as a default param
# value (default-param evaluation happens at class-creation time).
_UNSET: Any = object()


# =============================================================================
# SignalType — mirrors the SPEC §3.1 enum
# =============================================================================


class SignalType(StrEnum):
    """Classification of a friction signal per SPEC §3.1.

    Used as the ``signal_type`` field on reactive ``annotate()`` calls. The
    eight values are stable and additive-only until v1.0 (SPEC §13).
    """

    FAILURE = "failure"
    RETRY_LOOP = "retry_loop"
    DEAD_END = "dead_end"
    PARAMETER_CONFUSION = "parameter_confusion"
    SLOW_PERFORMANCE = "slow_performance"
    ABANDONMENT = "abandonment"
    FEATURE_GAP = "feature_gap"
    OTHER = "other"


# =============================================================================
# Internal — config loading
# =============================================================================


def _resolve_config_value(
    explicit: str | None, env_key: str, *, required: bool, name: str
) -> str | None:
    """Explicit kwargs win; env-var fallback supported.

    Returns the explicit value if non-None; else the env var; else None
    (raises ``ValueError`` if ``required`` and both sources are empty).
    """
    if explicit is not None:
        return explicit
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value
    if required:
        raise ValueError(
            f"{name} must be supplied explicitly or via the {env_key} environment variable"
        )
    return None


def _resolve_signal_type(signal_type: SignalType | str | None) -> str | None:
    """Validate + normalize ``signal_type`` to its canonical string form.

    Enum instance → ``.value``. Bare string → validated against the enum's
    member values (raises ``ValueError`` on miss to surface typos like
    ``"dead-end"`` instead of ``"dead_end"`` immediately, rather than silently
    shipping a non-standard signal that the Console worker will bucket as
    "other" or drop).
    """
    if signal_type is None:
        return None
    if isinstance(signal_type, SignalType):
        return signal_type.value
    valid = {m.value for m in SignalType}
    if signal_type not in valid:
        raise ValueError(
            f"signal_type {signal_type!r} is not a valid SignalType. "
            f"Valid values: {sorted(valid)}. Pass the SignalType enum (e.g., "
            f"SignalType.DEAD_END) for type-safety."
        )
    return signal_type


# =============================================================================
# Internal — sync-over-async bridge (Sentry/Datadog pattern)
# =============================================================================


class _SyncBridge:
    """Background daemon thread running a persistent asyncio event loop.

    Sync ``Client`` methods submit coroutines via ``run_coroutine_threadsafe``
    and block on the resulting ``Future``. Lets sync user code drive the async
    ``EventEmitter`` (which owns the buffer, retry loop, circuit breaker) without
    creating a new event loop per emit (each ``asyncio.run`` would invalidate
    the emitter's loop-bound state).
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="baton-sync-bridge"
        )
        self._thread.start()
        # Block until the loop is created and running. If the thread crashes
        # before the loop starts, this would hang — short timeout guards it.
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("baton-sync-bridge thread failed to start within 5s")

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            # Clean up any pending tasks before closing the loop.
            for task in asyncio.all_tasks(self._loop):
                task.cancel()
            self._loop.close()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Submit a coroutine to the bridge loop; block until result."""
        assert self._loop is not None, "bridge not initialized"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def stop(self) -> None:
        """Stop the loop + join the thread. Safe to call multiple times."""
        if self._loop is None or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)


# =============================================================================
# Trace — sync context manager for one logical tool call
# =============================================================================


class Trace:
    """One logical tool call's worth of events. Returned by ``client.trace(...)``.

    Lifecycle:

    - ``with client.trace(...) as trace:`` — emits ``tool_call_start``.
    - ``trace.with_params({...})`` — optional; attaches params to the start event
      *before* it ships. Must be called before the ``with`` block exits.
    - ``trace.observed(result=...)`` — record the outcome to emit on exit.
    - Exit (normal): emits ``tool_call_end`` with the observed outcome.
    - Exit (exception): emits ``tool_call_error`` automatically; re-raises.

    Multiple ``observed()`` calls — last wins; a ``UserWarning`` fires on
    subsequent calls. Exit without ``observed()`` — ``tool_call_end`` emits
    with ``result=None`` and a ``UserWarning``.
    """

    def __init__(
        self,
        *,
        client: Client,
        tool_name: str,
        intent: str | None,
        expected_outcome: str | None,
        workflow: str | None,
        consent_token: str | None,
        session_id: str | None,
    ) -> None:
        self._client = client
        self._tool_name = tool_name
        self._intent = intent
        self._expected_outcome = expected_outcome
        self._workflow = workflow
        # Per-trace consent_token overrides the Client-level default.
        self._consent_token: str = consent_token or client._consent_token
        # session_id per the SPEC §3.4 layered fallback. In library mode we
        # default to per-event (fresh UUID per trace); per-trace override
        # supports session-stitched mode when a caller passes an explicit id.
        self._session_id = session_id or str(uuid7())
        self._params: dict[str, Any] = {}
        self._observed_result: Any = _UNSET
        self._observed_error: tuple[str, str] | None = None
        self._start_seq: int | None = None
        self._call_started_at: float | None = None
        self._observed_warned = False

    @property
    def session_id(self) -> str:
        """The session_id this trace owns.

        Public so callers can correlate post-trace ``client.annotate(...)``
        calls with the trace they're about — or, more ergonomically, use
        ``trace.annotate(...)`` (below) which binds the session_id for you.
        """
        return self._session_id

    def with_params(self, params: dict[str, Any]) -> Self:
        """Attach params to the start event before it ships."""
        self._params = self._client._scrubber(params)
        # If start has already shipped (called inside the with block after
        # __enter__), this is a UserWarning — the params don't reach the
        # already-emitted event. Documented limitation.
        if self._start_seq is not None:
            import warnings

            warnings.warn(
                "Trace.with_params() called after the start event already shipped; "
                "params will not reach the emitted event. Call with_params() before "
                "the first awaited operation inside the with block, OR pass params "
                "via client.trace(..., params=...) in a future API version.",
                UserWarning,
                stacklevel=2,
            )
        return self

    def annotate(
        self,
        *,
        signal_type: SignalType | str | None = None,
        intent: str | None = None,
        expected_outcome: str | None = None,
        workflow: str | None = None,
        suggested_improvement: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Emit a reactive annotation bound to this trace's ``session_id``.

        Equivalent to ``client.annotate(session_id=trace.session_id, ...)`` but
        without the manual wiring. Use to attach a friction signal (the
        ``dead_end`` / ``retry_loop`` / etc. "ticket") to the same logical
        session as the trace — typically after the ``with`` block exits, once
        the caller has decided whether the outcome was friction-worthy.

        Sequence numbers continue from the trace's counter, so the ticket
        lands in correct order after the trace's ``end``/``error`` event.
        """
        self._client.annotate(
            signal_type=signal_type,
            intent=intent,
            expected_outcome=expected_outcome,
            workflow=workflow,
            suggested_improvement=suggested_improvement,
            context=context,
            session_id=self._session_id,
            consent_token=self._consent_token,
        )

    def observed(
        self,
        result: Any = _UNSET,
        *,
        error: BaseException | None = None,
        error_type: str | None = None,
        error_body: str | None = None,
    ) -> Self:
        """Record the outcome. Three modes:

        - ``observed(result=...)`` — success path.
        - ``observed(error=exc)`` — failure path (preferred). The trace derives
          ``error_type = type(exc).__name__`` and ``error_body = str(exc)``
          automatically. Use this when you've caught an exception inside the
          ``with`` block but want to continue (e.g., to emit a reactive
          annotation) rather than let it propagate.
        - ``observed(error_type=..., error_body=...)`` — failure path with
          explicit strings (when you only have stringified info, not an
          exception object).

        Explicit ``error_type``/``error_body`` win over values derived from
        ``error`` if both are passed.
        """
        if self._observed_result is not _UNSET or self._observed_error is not None:
            if not self._observed_warned:
                import warnings

                warnings.warn(
                    "Trace.observed() called multiple times; last call wins.",
                    UserWarning,
                    stacklevel=2,
                )
                self._observed_warned = True

        if error is not None:
            error_type = error_type or type(error).__name__
            error_body = error_body or str(error)

        if error_type is not None or error_body is not None:
            self._observed_error = (error_type or "Error", error_body or "")
            self._observed_result = _UNSET
        else:
            self._observed_result = self._client._scrubber(result)
            self._observed_error = None
        return self

    def __enter__(self) -> Self:
        self._call_started_at = monotonic()
        self._start_seq = self._client._next_seq(self._session_id)
        start_event = ToolCallStartEvent(
            tenant_id=self._client._vendor_id,
            session_id=self._session_id,
            sequence_number=self._start_seq,
            captured_at=datetime.now(UTC),
            consent_token=self._consent_token,
            agent_runtime=self._client._agent_runtime,
            payload=ToolCallStartPayload(
                tool_name=self._tool_name,
                params=self._params,
            ),
        )
        self._client._emit_sync(start_event)
        # Proactive annotation if intent/expected/workflow supplied.
        if self._intent or self._expected_outcome or self._workflow:
            ann_seq = self._client._next_seq(self._session_id)
            ann_event = AnnotationEvent(
                tenant_id=self._client._vendor_id,
                session_id=self._session_id,
                sequence_number=ann_seq,
                captured_at=datetime.now(UTC),
                consent_token=self._consent_token,
                agent_runtime=self._client._agent_runtime,
                payload=AnnotationPayload(
                    intent=self._intent,
                    expected_outcome=self._expected_outcome,
                    workflow=self._workflow,
                ),
            )
            self._client._emit_sync(ann_event)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        duration_ms = self._compute_duration_ms()
        end_seq = self._client._next_seq(self._session_id)

        if exc is not None:
            error_event = ToolCallErrorEvent(
                tenant_id=self._client._vendor_id,
                session_id=self._session_id,
                sequence_number=end_seq,
                captured_at=datetime.now(UTC),
                consent_token=self._consent_token,
                agent_runtime=self._client._agent_runtime,
                payload=ToolCallErrorPayload(
                    tool_name=self._tool_name,
                    error_type=exc.__class__.__name__,
                    error_body=self._client._scrubber(str(exc) or "".join(traceback.format_tb(tb))),
                    duration_ms=duration_ms,
                ),
            )
            self._client._emit_sync(error_event)
            return  # re-raise

        if self._observed_result is _UNSET and self._observed_error is None:
            import warnings

            warnings.warn(
                "Trace exited without observed() — emitting tool_call_end with result=None. "
                "Call observed(result=...) inside the with block to record the outcome.",
                UserWarning,
                stacklevel=2,
            )

        if self._observed_error is not None:
            # observed() was called with error_type/body — emit tool_call_error
            error_type, error_body = self._observed_error
            error_event = ToolCallErrorEvent(
                tenant_id=self._client._vendor_id,
                session_id=self._session_id,
                sequence_number=end_seq,
                captured_at=datetime.now(UTC),
                consent_token=self._consent_token,
                agent_runtime=self._client._agent_runtime,
                payload=ToolCallErrorPayload(
                    tool_name=self._tool_name,
                    error_type=error_type,
                    error_body=self._client._scrubber(error_body),
                    duration_ms=duration_ms,
                ),
            )
            self._client._emit_sync(error_event)
        else:
            end_event = ToolCallEndEvent(
                tenant_id=self._client._vendor_id,
                session_id=self._session_id,
                sequence_number=end_seq,
                captured_at=datetime.now(UTC),
                consent_token=self._consent_token,
                agent_runtime=self._client._agent_runtime,
                payload=ToolCallEndPayload(
                    tool_name=self._tool_name,
                    result=(self._observed_result if self._observed_result is not _UNSET else None),
                    duration_ms=duration_ms,
                ),
            )
            self._client._emit_sync(end_event)

    def _compute_duration_ms(self) -> int | None:
        if self._call_started_at is None:
            return None
        return int((monotonic() - self._call_started_at) * 1000)


# =============================================================================
# Client — sync
# =============================================================================


class Client:
    """Sync library API client. Drives async ``EventEmitter`` via a background
    daemon thread (``_SyncBridge``). One client instance per vendor process.
    """

    def __init__(
        self,
        *,
        sink: Sink,
        vendor_id: str | None = None,
        consent_token: str | None = None,
        agent_runtime: str = "python-library",
        scrubber: Any = identity_scrub,
    ) -> None:
        vendor_id_resolved = _resolve_config_value(
            vendor_id, "BATON_VENDOR_ID", required=True, name="vendor_id"
        )
        consent_token_resolved = _resolve_config_value(
            consent_token, "BATON_CONSENT_TOKEN", required=True, name="consent_token"
        )

        assert vendor_id_resolved is not None
        assert consent_token_resolved is not None

        self._vendor_id: str = vendor_id_resolved
        self._consent_token: str = consent_token_resolved
        self._agent_runtime: str = agent_runtime
        self._scrubber = scrubber

        # Sync mode uses a background thread + persistent loop bridge so the
        # sink's async primitives (locks, background drain tasks, httpx
        # clients) bind to one stable loop instead of a fresh one per emit.
        self._bridge = _SyncBridge()
        self._sink: Sink = sink

        # Per-session sequence counters. Library mode = per-event mode (each
        # Trace generates a fresh session_id), so each session_id has exactly
        # one trace's worth of events. Counter still maintained for consistency
        # with SPEC §11.4 (monotonic sequence_number per session).
        self._seq_counters: dict[str, int] = {}
        self._closed = False

    def trace(
        self,
        *,
        tool_name: str,
        intent: str | None = None,
        expected_outcome: str | None = None,
        workflow: str | None = None,
        params: dict[str, Any] | None = None,
        consent_token: str | None = None,
        session_id: str | None = None,
    ) -> Trace:
        """Open a trace for one logical tool call. Use as a context manager.

        Pass ``params`` here (not via ``trace.with_params()``) so they ship on
        the ``tool_call_start`` event in ``__enter__``. ``with_params()`` is
        retained for late-bound updates but emits a ``UserWarning`` because the
        start event has already shipped by the time the body runs.
        """
        if self._closed:
            raise RuntimeError("Client is closed")
        trace = Trace(
            client=self,
            tool_name=tool_name,
            intent=intent,
            expected_outcome=expected_outcome,
            workflow=workflow,
            consent_token=consent_token,
            session_id=session_id,
        )
        if params is not None:
            trace._params = self._scrubber(params)
        return trace

    def annotate(
        self,
        *,
        signal_type: SignalType | str | None = None,
        intent: str | None = None,
        expected_outcome: str | None = None,
        workflow: str | None = None,
        suggested_improvement: str | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        consent_token: str | None = None,
    ) -> None:
        """Emit a standalone annotation event (reactive friction signal, or
        proactive intent capture outside a ``trace()``).

        ``consent_token`` defaults to the Client-level token; pass an override
        when the annotation belongs to a different end-user than the Client's
        configured token (or when ``Trace.annotate(...)`` is forwarding a
        per-trace override).
        """
        if self._closed:
            raise RuntimeError("Client is closed")
        resolved_session = session_id or str(uuid7())
        resolved_consent = consent_token or self._consent_token
        seq = self._next_seq(resolved_session)
        signal_type_str = _resolve_signal_type(signal_type)
        event = AnnotationEvent(
            tenant_id=self._vendor_id,
            session_id=resolved_session,
            sequence_number=seq,
            captured_at=datetime.now(UTC),
            consent_token=resolved_consent,
            agent_runtime=self._agent_runtime,
            payload=AnnotationPayload(
                intent=intent,
                expected_outcome=expected_outcome,
                signal_type=signal_type_str,
                workflow=workflow,
                suggested_improvement=suggested_improvement,
                context=self._scrubber(context) if context else None,
            ),
        )
        self._emit_sync(event)

    def flush(self) -> None:
        """Block until pending events drain."""
        if self._closed:
            return
        self._bridge.run(self._sink.flush())

    def close(self) -> None:
        """Flush + close the sink + stop the bridge thread."""
        if self._closed:
            return
        try:
            self._bridge.run(self._sink.aclose())
        finally:
            self._closed = True
            self._bridge.stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # =========================================================================
    # Internal — used by Trace
    # =========================================================================

    def _emit_sync(self, event: Any) -> None:
        self._bridge.run(self._sink.write(event))

    def _next_seq(self, session_id: str) -> int:
        current = self._seq_counters.get(session_id, 0)
        self._seq_counters[session_id] = current + 1
        return current + 1


# =============================================================================
# AsyncTrace — async context manager equivalent
# =============================================================================


class AsyncTrace:
    """Async equivalent of ``Trace``. Returned by ``AsyncClient.trace(...)``."""

    def __init__(
        self,
        *,
        client: AsyncClient,
        tool_name: str,
        intent: str | None,
        expected_outcome: str | None,
        workflow: str | None,
        consent_token: str | None,
        session_id: str | None,
    ) -> None:
        self._client = client
        self._tool_name = tool_name
        self._intent = intent
        self._expected_outcome = expected_outcome
        self._workflow = workflow
        self._consent_token: str = consent_token or client._consent_token
        self._session_id = session_id or str(uuid7())
        self._params: dict[str, Any] = {}
        self._observed_result: Any = _UNSET
        self._observed_error: tuple[str, str] | None = None
        self._start_seq: int | None = None
        self._call_started_at: float | None = None
        self._observed_warned = False

    @property
    def session_id(self) -> str:
        """The session_id this trace owns. See ``Trace.session_id`` for usage."""
        return self._session_id

    def with_params(self, params: dict[str, Any]) -> Self:
        self._params = self._client._scrubber(params)
        if self._start_seq is not None:
            import warnings

            warnings.warn(
                "AsyncTrace.with_params() called after the start event already shipped; "
                "params will not reach the emitted event.",
                UserWarning,
                stacklevel=2,
            )
        return self

    async def annotate(
        self,
        *,
        signal_type: SignalType | str | None = None,
        intent: str | None = None,
        expected_outcome: str | None = None,
        workflow: str | None = None,
        suggested_improvement: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Async equivalent of ``Trace.annotate``. Binds the trace's
        ``session_id`` to the emitted annotation event.
        """
        await self._client.annotate(
            signal_type=signal_type,
            intent=intent,
            expected_outcome=expected_outcome,
            workflow=workflow,
            suggested_improvement=suggested_improvement,
            context=context,
            session_id=self._session_id,
            consent_token=self._consent_token,
        )

    def observed(
        self,
        result: Any = _UNSET,
        *,
        error: BaseException | None = None,
        error_type: str | None = None,
        error_body: str | None = None,
    ) -> Self:
        """Async equivalent of ``Trace.observed``. See that docstring for the
        three modes (result / error / error_type+error_body)."""
        if self._observed_result is not _UNSET or self._observed_error is not None:
            if not self._observed_warned:
                import warnings

                warnings.warn(
                    "AsyncTrace.observed() called multiple times; last call wins.",
                    UserWarning,
                    stacklevel=2,
                )
                self._observed_warned = True

        if error is not None:
            error_type = error_type or type(error).__name__
            error_body = error_body or str(error)

        if error_type is not None or error_body is not None:
            self._observed_error = (error_type or "Error", error_body or "")
            self._observed_result = _UNSET
        else:
            self._observed_result = self._client._scrubber(result)
            self._observed_error = None
        return self

    async def __aenter__(self) -> Self:
        self._call_started_at = monotonic()
        self._start_seq = self._client._next_seq(self._session_id)
        start_event = ToolCallStartEvent(
            tenant_id=self._client._vendor_id,
            session_id=self._session_id,
            sequence_number=self._start_seq,
            captured_at=datetime.now(UTC),
            consent_token=self._consent_token,
            agent_runtime=self._client._agent_runtime,
            payload=ToolCallStartPayload(
                tool_name=self._tool_name,
                params=self._params,
            ),
        )
        await self._client._emit(start_event)
        if self._intent or self._expected_outcome or self._workflow:
            ann_seq = self._client._next_seq(self._session_id)
            ann_event = AnnotationEvent(
                tenant_id=self._client._vendor_id,
                session_id=self._session_id,
                sequence_number=ann_seq,
                captured_at=datetime.now(UTC),
                consent_token=self._consent_token,
                agent_runtime=self._client._agent_runtime,
                payload=AnnotationPayload(
                    intent=self._intent,
                    expected_outcome=self._expected_outcome,
                    workflow=self._workflow,
                ),
            )
            await self._client._emit(ann_event)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        duration_ms = (
            int((monotonic() - self._call_started_at) * 1000)
            if self._call_started_at is not None
            else None
        )
        end_seq = self._client._next_seq(self._session_id)

        if exc is not None:
            error_event = ToolCallErrorEvent(
                tenant_id=self._client._vendor_id,
                session_id=self._session_id,
                sequence_number=end_seq,
                captured_at=datetime.now(UTC),
                consent_token=self._consent_token,
                agent_runtime=self._client._agent_runtime,
                payload=ToolCallErrorPayload(
                    tool_name=self._tool_name,
                    error_type=exc.__class__.__name__,
                    error_body=self._client._scrubber(str(exc) or "".join(traceback.format_tb(tb))),
                    duration_ms=duration_ms,
                ),
            )
            await self._client._emit(error_event)
            return

        if self._observed_result is _UNSET and self._observed_error is None:
            import warnings

            warnings.warn(
                "AsyncTrace exited without observed() — emitting tool_call_end with result=None.",
                UserWarning,
                stacklevel=2,
            )

        if self._observed_error is not None:
            error_type, error_body = self._observed_error
            error_event = ToolCallErrorEvent(
                tenant_id=self._client._vendor_id,
                session_id=self._session_id,
                sequence_number=end_seq,
                captured_at=datetime.now(UTC),
                consent_token=self._consent_token,
                agent_runtime=self._client._agent_runtime,
                payload=ToolCallErrorPayload(
                    tool_name=self._tool_name,
                    error_type=error_type,
                    error_body=self._client._scrubber(error_body),
                    duration_ms=duration_ms,
                ),
            )
            await self._client._emit(error_event)
        else:
            end_event = ToolCallEndEvent(
                tenant_id=self._client._vendor_id,
                session_id=self._session_id,
                sequence_number=end_seq,
                captured_at=datetime.now(UTC),
                consent_token=self._consent_token,
                agent_runtime=self._client._agent_runtime,
                payload=ToolCallEndPayload(
                    tool_name=self._tool_name,
                    result=(self._observed_result if self._observed_result is not _UNSET else None),
                    duration_ms=duration_ms,
                ),
            )
            await self._client._emit(end_event)


# =============================================================================
# AsyncClient — async
# =============================================================================


class AsyncClient:
    """Async equivalent of ``Client``. Directly drives the async ``EventEmitter``
    (no thread bridge needed since the caller is already async).
    """

    def __init__(
        self,
        *,
        sink: Sink,
        vendor_id: str | None = None,
        consent_token: str | None = None,
        agent_runtime: str = "python-library",
        scrubber: Any = identity_scrub,
    ) -> None:
        vendor_id_resolved = _resolve_config_value(
            vendor_id, "BATON_VENDOR_ID", required=True, name="vendor_id"
        )
        consent_token_resolved = _resolve_config_value(
            consent_token, "BATON_CONSENT_TOKEN", required=True, name="consent_token"
        )

        assert vendor_id_resolved is not None
        assert consent_token_resolved is not None

        self._vendor_id: str = vendor_id_resolved
        self._consent_token: str = consent_token_resolved
        self._agent_runtime: str = agent_runtime
        self._scrubber = scrubber

        self._sink: Sink = sink
        self._seq_counters: dict[str, int] = {}
        self._closed = False

    def trace(
        self,
        *,
        tool_name: str,
        intent: str | None = None,
        expected_outcome: str | None = None,
        workflow: str | None = None,
        params: dict[str, Any] | None = None,
        consent_token: str | None = None,
        session_id: str | None = None,
    ) -> AsyncTrace:
        if self._closed:
            raise RuntimeError("AsyncClient is closed")
        trace = AsyncTrace(
            client=self,
            tool_name=tool_name,
            intent=intent,
            expected_outcome=expected_outcome,
            workflow=workflow,
            consent_token=consent_token,
            session_id=session_id,
        )
        if params is not None:
            trace._params = self._scrubber(params)
        return trace

    async def annotate(
        self,
        *,
        signal_type: SignalType | str | None = None,
        intent: str | None = None,
        expected_outcome: str | None = None,
        workflow: str | None = None,
        suggested_improvement: str | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
        consent_token: str | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("AsyncClient is closed")
        resolved_session = session_id or str(uuid7())
        resolved_consent = consent_token or self._consent_token
        seq = self._next_seq(resolved_session)
        signal_type_str = _resolve_signal_type(signal_type)
        event = AnnotationEvent(
            tenant_id=self._vendor_id,
            session_id=resolved_session,
            sequence_number=seq,
            captured_at=datetime.now(UTC),
            consent_token=resolved_consent,
            agent_runtime=self._agent_runtime,
            payload=AnnotationPayload(
                intent=intent,
                expected_outcome=expected_outcome,
                signal_type=signal_type_str,
                workflow=workflow,
                suggested_improvement=suggested_improvement,
                context=self._scrubber(context) if context else None,
            ),
        )
        await self._emit(event)

    async def flush(self) -> None:
        if self._closed:
            return
        await self._sink.flush()

    async def aclose(self) -> None:
        if self._closed:
            return
        try:
            await self._sink.aclose()
        finally:
            self._closed = True

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
    # Internal
    # =========================================================================

    async def _emit(self, event: Any) -> None:
        await self._sink.write(event)

    def _next_seq(self, session_id: str) -> int:
        current = self._seq_counters.get(session_id, 0)
        self._seq_counters[session_id] = current + 1
        return current + 1
