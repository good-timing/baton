"""Tool-handler wrapping — the capture mechanism for the official mcp SDK.

``mcp.server.fastmcp.FastMCP`` exposes no middleware hook. Instead, we wrap
each registered ``Tool.run`` method in place. Per the spike, this is stable
across mcp v1.10 → v1.27 (verified by the CI matrix).

Why wrap ``Tool.run`` (instead of ``Tool.fn`` as the 0.2.x adapter did):
- We receive the raw ``arguments`` dict directly — no inspect.signature
  binding gymnastics to map positional args back to parameter names.
- We receive the request ``context`` directly — that's where the MCP
  ``_meta`` lives, which we forward as the event envelope's ``runtime_meta``
  field per SPEC §11.4.1 (the primitive the Console worker uses for cycle
  correlation more precise than session_id alone).
- ``Tool.run`` is always async — no sync→async bridging via
  ``asyncio.to_thread``, no need to flip ``Tool.is_async``.
- The original ``Tool.run`` already handles sync vs. async ``fn`` dispatch
  through ``fn_metadata.call_fn_with_arg_validation`` — we instrument
  around it without owning that dispatch.

Strategy:
1. After ``install_baton``, iterate ``_tool_manager._tools`` and (a) inject the
   ``baton_intent`` param into each tool's advertised schema and (b) replace
   ``tool.run`` with a wrapper that strips the param and emits Baton events.
2. Monkey-patch ``_tool_manager.add_tool`` so tools registered AFTER
   ``install_baton`` are also injected + wrapped automatically.

The wrapped run emits ``tool_call_start`` before invocation,
``tool_call_end`` on success, ``tool_call_error`` on exception. Re-raises
the exception so the caller's error path is unchanged. When ``Tool.run``
wraps the original exception in ``ToolError`` (which it does), the event
records the unwrapped ``__cause__`` so ``error_type`` reflects the real
exception class (e.g., ``RuntimeError``, not ``ToolError``).

**Intent-param injection (mirrors the FastMCP middleware + baton-proxy).**
The official SDK exposes no ``on_list_tools`` middleware hook, so instead of
injecting into a per-request tool list we mutate each ``Tool.parameters`` dict
once, in place, at install time — that dict is exactly what ``FastMCP.list_tools``
advertises as ``inputSchema``. The reserved, namespaced ``baton_intent`` param
is stripped back out in the wrapper before the vendor handler validates its
arguments, so the tool never sees it. This is the capture path that survives
runtimes which drop ``instructions`` (notably Claude Desktop). The session's
first injected intent also synthesises one proactive annotation, coordinated
with the annotation tool via a shared ``ProactiveTracker`` so a session opens at
most one proactive.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from baton._state import ProactiveTracker, SessionCounter
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
from baton.integrations._llm_text import (
    INTENT_PARAM_NAME,
    INTENT_SOURCE_PARAM,
    build_intent_param_description,
)
from baton.integrations.mcp._registry import get_tool_manager, get_tool_registry
from baton.scrub import identity_scrub
from baton.sinks import Sink, safe_write

logger = logging.getLogger(__name__)

# Sentinel attribute set on wrapped run methods so repeated re-scans don't
# double-wrap. Tools added via the patched add_tool are checked against
# this before wrapping.
_WRAPPED_SENTINEL = "_baton_wrapped"


def install_wraps(
    mcp: Any,
    *,
    tenant_id: str,
    vendor_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    fallback_session_id: str,
    default_agent_runtime: str = "unknown",
    scrubber: Callable[[Any], Any] = identity_scrub,
    annotation_tool_name: str | None = None,
    intent_param_mode: str = "optional",
    proactive_tracker: ProactiveTracker | None = None,
) -> None:
    """Inject + wrap all currently-registered tools AND future registrations."""
    tracker = proactive_tracker or ProactiveTracker()
    # tool_name -> "injected" | "native". Populated as tools are injected; read
    # in the wrapper to decide strip-vs-forward. A plain dict (no lock) is safe:
    # all access is on the one asyncio loop, so no statement interleaves.
    param_registry: dict[str, str] = {}
    emit_before, emit_after, emit_error, emit_proactive = _make_emitters(
        tenant_id=tenant_id,
        vendor_id=vendor_id,
        consent_token=consent_token,
        sink=sink,
        counter=counter,
        fallback_session_id=fallback_session_id,
        default_agent_runtime=default_agent_runtime,
        scrubber=scrubber,
    )

    def _maybe_wrap_entry(name: str, tool: Any) -> None:
        # Skip the annotation tool — its handler emits its own annotation
        # event with the structured payload, and it takes ``intent`` explicitly
        # so it needs no injected ``baton_intent``.
        if annotation_tool_name is not None and name == annotation_tool_name:
            return
        # Inject BEFORE wrapping so the advertised schema carries the param on
        # the very first tools/list. Idempotent: a re-scan (via add_tool) skips
        # tools already in the registry rather than re-detecting them "native".
        if intent_param_mode != "off" and name not in param_registry:
            try:
                disposition = _inject_intent_param(tool, intent_param_mode)
            except Exception:
                logger.exception("baton: intent-param injection failed for a tool")
            else:
                if disposition is not None:
                    param_registry[name] = disposition
        if getattr(tool.run, _WRAPPED_SENTINEL, False):
            return
        # mcp's Tool is a Pydantic BaseModel; `run` is a method, not a field,
        # so plain attribute assignment is rejected. Bypass Pydantic with
        # object.__setattr__ to install an instance-level shadow.
        object.__setattr__(
            tool,
            "run",
            _wrap_tool_run(
                name,
                tool,
                emit_before,
                emit_after,
                emit_error,
                emit_proactive,
                scrubber,
                intent_param_mode=intent_param_mode,
                param_registry=param_registry,
                tracker=tracker,
                session_id=fallback_session_id,
            ),
        )

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
        # Walk the full registry afterwards — add_tool may insert under a
        # derived name we can't predict from the args alone.
        for name, tool in list(registry.items()):
            _maybe_wrap_entry(name, tool)
        return result

    manager.add_tool = add_tool_with_wrap


# =============================================================================
# Internals — intent-param injection + strip
# =============================================================================


def _inject_intent_param(tool: Any, intent_param_mode: str) -> str | None:
    """Inject ``baton_intent`` into ``tool.parameters`` in place; return the
    disposition (``"injected"`` / ``"native"`` / ``None`` when unschemable).

    Unlike the FastMCP middleware — which deep-copies on every ``on_list_tools``
    — this runs once at install and mutates the tool's canonical schema dict
    directly, because that dict IS what ``FastMCP.list_tools`` advertises as
    ``inputSchema``. A tool that already declares ``baton_intent`` is left
    untouched and recorded ``"native"`` so the wrapper forwards the caller's
    value instead of stripping it. Mirrors the proxy's injector."""
    schema = tool.parameters
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties")
    if isinstance(props, dict) and INTENT_PARAM_NAME in props:
        return "native"
    new_props = schema.setdefault("properties", {})
    if not isinstance(new_props, dict):
        return None
    new_props[INTENT_PARAM_NAME] = {
        "type": "string",
        "description": build_intent_param_description(),
    }
    if intent_param_mode == "required":
        required = schema.get("required")
        if isinstance(required, list):
            if INTENT_PARAM_NAME not in required:
                required.append(INTENT_PARAM_NAME)
        else:
            schema["required"] = [INTENT_PARAM_NAME]
    return "injected"


def _extract_intent_param(
    tool_name: str,
    arguments: dict[str, Any],
    intent_param_mode: str,
    param_registry: dict[str, str],
) -> str | None:
    """Pop the injected intent from ``arguments`` in place; return its value.

    Mutating in place is what keeps the value off the vendor handler — the same
    dict is forwarded to ``original_run``. Registry dispositions mirror the
    proxy: ``"native"`` → the param is the vendor's, forward untouched; unknown
    (cold registry — a call arrived before the tool was scanned) → strip with a
    warning, safe only because the name is namespaced. Never raises."""
    if intent_param_mode == "off":
        return None
    if INTENT_PARAM_NAME not in arguments:
        return None
    disposition = param_registry.get(tool_name)
    if disposition == "native":
        return None
    if disposition is None:
        logger.warning(
            "baton: stripping %s from unlisted tool %r (cold registry)",
            INTENT_PARAM_NAME,
            tool_name,
        )
    raw = arguments.pop(INTENT_PARAM_NAME, None)
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


# =============================================================================
# Internals — wrap
# =============================================================================


def _wrap_tool_run(
    name: str,
    tool: Any,
    emit_before: Callable[
        [str, dict[str, Any], dict[str, Any] | None, str | None], Awaitable[None]
    ],
    emit_after: Callable[[str, Any, float, dict[str, Any] | None], Awaitable[None]],
    emit_error: Callable[[str, BaseException, float, dict[str, Any] | None], Awaitable[None]],
    emit_proactive: Callable[[str, str, dict[str, Any] | None], Awaitable[None]],
    scrubber: Callable[[Any], Any],
    *,
    intent_param_mode: str,
    param_registry: dict[str, str],
    tracker: ProactiveTracker,
    session_id: str,
) -> Callable[..., Awaitable[Any]]:
    """Build an async wrapper around ``tool.run`` that strips the injected
    intent param and emits Baton events.

    Signature mirrors mcp's ``Tool.run``: ``(arguments, context=None,
    convert_result=False) -> Any``. We instrument around the original; we
    do not own argument validation or sync/async dispatch.
    """
    original_run = tool.run  # bound method on this tool instance

    async def wrapper(
        arguments: dict[str, Any] | None = None,
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        # Strip the injected intent IN PLACE, before snapshotting params —
        # ``arguments`` is the same object forwarded to the vendor handler, so
        # the strip keeps ``baton_intent`` off the tool AND out of the captured
        # ``params`` (which must equal the vendor-visible arguments).
        call_intent: str | None = None
        if isinstance(arguments, dict):
            call_intent = _extract_intent_param(name, arguments, intent_param_mode, param_registry)
        scrubbed_intent = scrubber(call_intent) if call_intent is not None else None

        params = dict(arguments or {})
        meta_dict = _extract_meta_from_context(context)
        scrubbed_meta = scrubber(meta_dict) if meta_dict is not None else None

        # The session's FIRST injected intent also becomes a proactive
        # annotation, sequenced BEFORE the tool_call_start it explains. ``claim``
        # dedups per session and is suppressed when a real annotation-tool
        # proactive already fired. Later param intents ride only the start event.
        if scrubbed_intent is not None and tracker.claim(session_id):
            await emit_proactive(name, scrubbed_intent, scrubbed_meta)

        await emit_before(name, scrubber(params), scrubbed_meta, scrubbed_intent)
        called_at = monotonic()
        try:
            result = await original_run(arguments, context=context, convert_result=convert_result)
        except BaseException as exc:
            # mcp's Tool.run does `raise ToolError(...) from e` — surface the
            # original __cause__ when present so error_type reflects the real
            # exception class the vendor's fn actually raised.
            original_exc = exc.__cause__ if exc.__cause__ is not None else exc
            await emit_error(name, original_exc, monotonic() - called_at, scrubbed_meta)
            raise
        await emit_after(name, result, monotonic() - called_at, scrubbed_meta)
        return result

    setattr(wrapper, _WRAPPED_SENTINEL, True)
    return wrapper


def _extract_meta_from_context(context: Any) -> dict[str, Any] | None:
    """Pull the wire ``_meta`` dict from ``mcp.server.fastmcp.Context``.

    Context is None when no client meta is available (rare; the MCP wire
    protocol normally surfaces at least a ``progressToken``). Returns None
    safely on any attribute miss — meta capture is best-effort.
    """
    if context is None:
        return None
    try:
        rc = context.request_context
        if rc is None:
            return None
        meta = rc.meta
    except (AttributeError, ValueError):
        # `request_context` raises ValueError when accessed outside a real
        # MCP request (e.g., when the wrapped tool is invoked via
        # mcp.call_tool() from test or programmatic code with no live wire).
        # Treat as "no meta available" — best-effort capture per SPEC §11.4.1.
        return None
    if meta is None:
        return None
    # mcp's RequestParams.Meta is a pydantic model; dump as dict using aliases
    # so namespaced keys (e.g., "claudecode/toolUseId") survive intact.
    if isinstance(meta, dict):
        return meta
    if hasattr(meta, "model_dump"):
        return meta.model_dump(by_alias=True)  # type: ignore[no-any-return]
    return None


def _make_emitters(
    *,
    tenant_id: str,
    vendor_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    fallback_session_id: str,
    default_agent_runtime: str,
    scrubber: Callable[[Any], Any],
) -> tuple[
    Callable[[str, dict[str, Any], dict[str, Any] | None, str | None], Awaitable[None]],
    Callable[[str, Any, float, dict[str, Any] | None], Awaitable[None]],
    Callable[[str, BaseException, float, dict[str, Any] | None], Awaitable[None]],
    Callable[[str, str, dict[str, Any] | None], Awaitable[None]],
]:
    """Build four async emitters: ``tool_call_start`` / ``_end`` / ``_error``
    plus the synthesised-proactive ``annotation``.

    Session-id: still the SDK fallback per-process UUID. The Console worker
    uses ``runtime_meta`` (now populated below) to derive finer per-cycle
    correlation per SPEC §11.5 — this adapter no longer needs to invent it.
    """

    async def _seq() -> int:
        return await counter.next(fallback_session_id)

    async def emit_proactive(name: str, intent: str, runtime_meta: dict[str, Any] | None) -> None:
        await safe_write(
            sink,
            AnnotationEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=fallback_session_id,
                sequence_number=await _seq(),
                captured_at=datetime.now(UTC),
                agent_runtime=default_agent_runtime,
                runtime_meta=runtime_meta,
                payload=AnnotationPayload(
                    intent=intent,
                    intent_source=INTENT_SOURCE_PARAM,
                    tool_name=name,
                ),
            ),
            logger,
        )

    async def emit_before(
        name: str,
        params: dict[str, Any],
        runtime_meta: dict[str, Any] | None,
        call_intent: str | None,
    ) -> None:
        await safe_write(
            sink,
            ToolCallStartEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=fallback_session_id,
                sequence_number=await _seq(),
                captured_at=datetime.now(UTC),
                agent_runtime=default_agent_runtime,
                runtime_meta=runtime_meta,
                payload=ToolCallStartPayload(
                    tool_name=name,
                    params=params,
                    call_intent=call_intent,
                    intent_source=INTENT_SOURCE_PARAM if call_intent is not None else None,
                ),
            ),
            logger,
        )

    async def emit_after(
        name: str, result: Any, duration_s: float, runtime_meta: dict[str, Any] | None
    ) -> None:
        await safe_write(
            sink,
            ToolCallEndEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=fallback_session_id,
                sequence_number=await _seq(),
                captured_at=datetime.now(UTC),
                agent_runtime=default_agent_runtime,
                runtime_meta=runtime_meta,
                payload=ToolCallEndPayload(
                    tool_name=name,
                    result=scrubber(_result_to_jsonable(result)),
                    duration_ms=int(duration_s * 1000),
                ),
            ),
            logger,
        )

    async def emit_error(
        name: str, exc: BaseException, duration_s: float, runtime_meta: dict[str, Any] | None
    ) -> None:
        await safe_write(
            sink,
            ToolCallErrorEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=fallback_session_id,
                sequence_number=await _seq(),
                captured_at=datetime.now(UTC),
                agent_runtime=default_agent_runtime,
                runtime_meta=runtime_meta,
                payload=ToolCallErrorPayload(
                    tool_name=name,
                    error_type=type(exc).__name__,
                    error_body=str(scrubber(str(exc)))[:2000],
                    duration_ms=int(duration_s * 1000),
                ),
            ),
            logger,
        )

    return emit_before, emit_after, emit_error, emit_proactive


def _result_to_jsonable(result: Any) -> Any:
    """Best-effort conversion of any tool result to a JSON-serializable shape.

    When ``Tool.run`` is called with ``convert_result=True`` (which mcp's
    ``FastMCP.call_tool`` does), the return is the wire-format tuple
    ``(content_list, structured_result_dict)`` where ``structured_result_dict``
    typically looks like ``{"result": <the fn's return value>}``. Unwrap the
    structured value so the ``tool_call_end.result`` field captures the
    developer-meaningful return, not the MCP wire envelope.
    """
    if result is None:
        return None
    # Unwrap (content, structured_result) tuples from convert_result=True path.
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        if isinstance(structured, dict) and "result" in structured:
            return _result_to_jsonable(structured["result"])
        # Fallback: serialize the content list.
        return _result_to_jsonable(content)
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if isinstance(result, (str, int, float, bool, list, dict)):
        return result
    return str(result)
