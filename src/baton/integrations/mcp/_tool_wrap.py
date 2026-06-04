"""Tool-handler wrapping — the capture mechanism for the official mcp SDK.

``mcp.server.fastmcp.FastMCP`` exposes no middleware hook. Instead, we
wrap each registered tool's ``fn`` in place. Per the spike, this is stable
across mcp v1.10 → v1.27.

Strategy:
1. After ``install_baton``, iterate ``_tool_manager._tools`` and wrap each
   ``Tool.fn`` with an async wrapper that emits Baton events.
2. Monkey-patch ``_tool_manager.add_tool`` so tools registered AFTER
   ``install_baton`` are also wrapped automatically.
3. For tools whose original ``fn`` was sync, the wrapper is async (so we
   can ``await sink.write(...)``), runs the original via
   ``asyncio.to_thread``, and we flip ``Tool.is_async = True`` so the
   ``Tool.run`` dispatcher invokes it via ``await``.

The wrapped fn emits ``tool_call_start`` before invocation,
``tool_call_end`` on success, ``tool_call_error`` on exception. Re-raises
the exception so the caller's error path is unchanged.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from baton._state import SessionCounter
from baton.events import (
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallErrorEvent,
    ToolCallErrorPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from baton.integrations.mcp._registry import get_tool_manager, get_tool_registry
from baton.scrub import identity_scrub
from baton.sinks import Sink

# Sentinel attribute set on wrapped fns so repeated re-scans don't
# double-wrap. Tools added via the patched add_tool are checked against
# this before wrapping.
_WRAPPED_SENTINEL = "_baton_wrapped"


def install_wraps(
    mcp: Any,
    *,
    tenant_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    fallback_session_id: str,
    default_agent_runtime: str = "unknown",
    scrubber: Callable[[Any], Any] = identity_scrub,
    annotation_tool_name: str | None = None,
) -> None:
    """Wrap all currently-registered tools AND future registrations on ``mcp``."""
    emit_before, emit_after, emit_error = _make_emitters(
        tenant_id=tenant_id,
        consent_token=consent_token,
        sink=sink,
        counter=counter,
        fallback_session_id=fallback_session_id,
        default_agent_runtime=default_agent_runtime,
        scrubber=scrubber,
    )

    def _maybe_wrap_entry(name: str, tool: Any) -> None:
        # Skip the annotation tool — its handler emits its own annotation
        # event with the structured payload.
        if annotation_tool_name is not None and name == annotation_tool_name:
            return
        if getattr(tool.fn, _WRAPPED_SENTINEL, False):
            return
        wrapped = _wrap_tool_fn(name, tool, emit_before, emit_after, emit_error)
        tool.fn = wrapped
        # Force async dispatch regardless of original; the wrapper is async.
        tool.is_async = True

    # 1. Wrap all currently-registered tools.
    registry = get_tool_registry(mcp)
    for name, tool in list(registry.items()):
        _maybe_wrap_entry(name, tool)

    # 2. Patch add_tool so future registrations are wrapped on insert.
    manager = get_tool_manager(mcp)
    original_add_tool = manager.add_tool

    @functools.wraps(original_add_tool)
    def add_tool_with_wrap(*args: Any, **kwargs: Any) -> Any:
        result = original_add_tool(*args, **kwargs)
        # Walk the full registry afterwards — add_tool may insert under
        # a derived name we can't predict from the args alone.
        for name, tool in list(registry.items()):
            _maybe_wrap_entry(name, tool)
        return result

    manager.add_tool = add_tool_with_wrap


# =============================================================================
# Internals
# =============================================================================


def _wrap_tool_fn(
    name: str,
    tool: Any,
    emit_before: Callable[[str, dict[str, Any]], Awaitable[None]],
    emit_after: Callable[[str, Any, float], Awaitable[None]],
    emit_error: Callable[[str, BaseException, float], Awaitable[None]],
) -> Callable[..., Awaitable[Any]]:
    """Build an async wrapper around ``tool.fn`` that emits Baton events.

    Sync originals are bridged to async via ``asyncio.to_thread`` so we
    can ``await`` sink writes around the call.
    """
    original = tool.fn
    was_async = bool(getattr(tool, "is_async", False))
    # Capture the original signature so we can bind *args back to parameter
    # names — the mcp SDK calls validated handlers via positional args, so
    # the wrapper's **kwargs alone would miss most of the params.
    sig = inspect.signature(original)

    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        await emit_before(name, _bind_params(sig, args, kwargs))
        called_at = monotonic()
        try:
            if was_async:
                result = await original(*args, **kwargs)
            else:
                # Bridge sync original into async context for emission.
                # Bind kwargs into a no-arg callable so to_thread can run it.
                bound: Callable[[], Any] = functools.partial(original, *args, **kwargs)
                result = await asyncio.to_thread(bound)
        except BaseException as exc:
            await emit_error(name, exc, monotonic() - called_at)
            raise
        await emit_after(name, result, monotonic() - called_at)
        return result

    setattr(wrapper, _WRAPPED_SENTINEL, True)
    return wrapper


def _make_emitters(
    *,
    tenant_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    fallback_session_id: str,
    default_agent_runtime: str,
    scrubber: Callable[[Any], Any],
) -> tuple[
    Callable[[str, dict[str, Any]], Awaitable[None]],
    Callable[[str, Any, float], Awaitable[None]],
    Callable[[str, BaseException, float], Awaitable[None]],
]:
    """Build three async emitters: ``tool_call_start`` / ``_end`` / ``_error``.

    Session-id: the official ``mcp.server.fastmcp`` Context exposes a
    session via ``ctx.request_context.session`` but the Tool.fn we wrap
    doesn't receive the Context unless the original opted in. To stay
    correct without overreaching the spike scope, we use the SDK fallback
    session-id for every event. Vendors who need true per-session
    correlation can pass an explicit ``default_agent_runtime`` and rely on
    downstream session-id derivation. (Follow-up: thread Context through
    the wrapper when the tool's signature includes a Context kwarg.)
    """

    async def _seq() -> int:
        return await counter.next(fallback_session_id)

    async def emit_before(name: str, params: dict[str, Any]) -> None:
        await sink.write(
            ToolCallStartEvent(
                tenant_id=tenant_id,
                consent_token=consent_token,
                session_id=fallback_session_id,
                sequence_number=await _seq(),
                captured_at=datetime.now(UTC),
                agent_runtime=default_agent_runtime,
                payload=ToolCallStartPayload(
                    tool_name=name,
                    params=scrubber(params),
                ),
            )
        )

    async def emit_after(name: str, result: Any, duration_s: float) -> None:
        await sink.write(
            ToolCallEndEvent(
                tenant_id=tenant_id,
                consent_token=consent_token,
                session_id=fallback_session_id,
                sequence_number=await _seq(),
                captured_at=datetime.now(UTC),
                agent_runtime=default_agent_runtime,
                payload=ToolCallEndPayload(
                    tool_name=name,
                    result=scrubber(_result_to_jsonable(result)),
                    duration_ms=int(duration_s * 1000),
                ),
            )
        )

    async def emit_error(name: str, exc: BaseException, duration_s: float) -> None:
        await sink.write(
            ToolCallErrorEvent(
                tenant_id=tenant_id,
                consent_token=consent_token,
                session_id=fallback_session_id,
                sequence_number=await _seq(),
                captured_at=datetime.now(UTC),
                agent_runtime=default_agent_runtime,
                payload=ToolCallErrorPayload(
                    tool_name=name,
                    error_type=type(exc).__name__,
                    error_body=str(scrubber(str(exc)))[:2000],
                    duration_ms=int(duration_s * 1000),
                ),
            )
        )

    return emit_before, emit_after, emit_error


def _bind_params(
    sig: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Map positional + keyword args back to {param_name: value}.

    Falls back to a plain kwargs dict if binding fails (e.g., extra args
    the signature doesn't declare); never raises into the emission path.
    """
    try:
        bound = sig.bind_partial(*args, **kwargs)
        return dict(bound.arguments)
    except TypeError:
        return dict(kwargs)


def _result_to_jsonable(result: Any) -> Any:
    """Best-effort conversion of any tool result to a JSON-serializable shape."""
    if result is None:
        return None
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if isinstance(result, (str, int, float, bool, list, dict)):
        return result
    return str(result)
