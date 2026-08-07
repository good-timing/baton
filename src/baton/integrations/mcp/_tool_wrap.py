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
   ``user_goal``/``expected_result`` params into each tool's advertised schema
   and (b) replace ``tool.run`` with a wrapper that strips them and emits
   Baton events.
2. Monkey-patch ``_tool_manager.add_tool`` so tools registered AFTER
   ``install_baton`` are also injected + wrapped automatically.

The wrapped run emits ``tool_call_start`` before invocation,
``tool_call_end`` on success, ``tool_call_error`` on exception. Re-raises
the exception so the caller's error path is unchanged. When ``Tool.run``
wraps the original exception in ``ToolError`` (which it does), the event
records the unwrapped ``__cause__`` so ``error_type`` reflects the real
exception class (e.g., ``RuntimeError``, not ``ToolError``).

**MRTR (multi-round-trip calls, mcp>=2.0).** A handler can pause mid-flight
and return ``InputRequiredResult`` to ask the client for more input before
the call actually completes; the client then retries the same logical call,
carrying its answers via ``Context.input_responses``/``request_state``. A
paused round is not a completion, so it gets no ``tool_call_end``
(``_is_mrtr_pause``); a round that's continuing a prior pause is not a new
call, so it gets no new ``tool_call_start`` (``_is_mrtr_continuation``) —
otherwise a 3-round exchange would misreport as one dangling start, one
spurious mid-sequence start+end pair, and one real completion. Whichever
round eventually returns a real result (or errors) gets the one true
``tool_call_end``/``tool_call_error``. Detection is duck-typed on the wire
shape, not an ``mcp_types`` import, so it's inert (always False) on mcp<2.0.

**Intent-param injection (mirrors the FastMCP middleware + baton-extmcp).**
The official SDK exposes no ``on_list_tools`` middleware hook, so instead of
injecting into a per-request tool list we mutate each ``Tool.parameters`` dict
once, in place, at install time — that dict is exactly what ``FastMCP.list_tools``
advertises as ``inputSchema``. The vendor-neutral ``user_goal``/``expected_result``
params (white-label rule — see ``integrations._llm_text``) are stripped back out
in the wrapper before the vendor handler validates its arguments, so the tool
never sees them. This is the capture path that survives runtimes which drop
``instructions`` (notably Claude Desktop). The session's first injected
``user_goal`` also synthesises one proactive annotation (carrying
``expected_result`` too, if present), coordinated with the annotation tool via
a shared ``ProactiveTracker`` so a session opens at most one proactive.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
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
from baton.integrations._config import (
    ResolveSessionIdHook,
    SessionResolutionContext,
    resolve_via_hook,
)
from baton.integrations._llm_text import (
    EXPECTED_RESULT_PARAM_NAME,
    INTENT_SOURCE_PARAM,
    USER_GOAL_PARAM_NAME,
    build_expected_result_param_description,
    build_user_goal_param_description,
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
    resolve_session_id_hook: ResolveSessionIdHook | None = None,
) -> None:
    """Inject + wrap all currently-registered tools AND future registrations."""
    tracker = proactive_tracker or ProactiveTracker()
    # tool_name -> {param_name: "injected" | "native"}. Populated as tools are
    # injected; read in the wrapper to decide strip-vs-forward, per param,
    # independently. A plain dict (no lock) is safe: all access is on the one
    # asyncio loop, so no statement interleaves.
    param_registry: dict[str, dict[str, str]] = {}
    emit_before, emit_after, emit_error, emit_proactive = _make_emitters(
        tenant_id=tenant_id,
        vendor_id=vendor_id,
        consent_token=consent_token,
        sink=sink,
        counter=counter,
        default_agent_runtime=default_agent_runtime,
        scrubber=scrubber,
    )

    def _maybe_wrap_entry(name: str, tool: Any) -> None:
        # Skip the annotation tool — its handler emits its own annotation
        # event with the structured payload, and it takes ``intent`` explicitly
        # so it needs no injected goal params.
        if annotation_tool_name is not None and name == annotation_tool_name:
            return
        # Inject BEFORE wrapping so the advertised schema carries the params on
        # the very first tools/list. Idempotent: a re-scan (via add_tool) skips
        # tools already in the registry rather than re-detecting them "native".
        if intent_param_mode != "off" and name not in param_registry:
            try:
                dispositions = _inject_goal_params(tool, intent_param_mode)
            except Exception:
                logger.exception("baton: intent-param injection failed for a tool")
            else:
                if dispositions:
                    param_registry[name] = dispositions
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
                fallback_session_id=fallback_session_id,
                resolve_session_id_hook=resolve_session_id_hook,
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


def _inject_goal_params(tool: Any, intent_param_mode: str) -> dict[str, str]:
    """Inject ``user_goal``/``expected_result`` into ``tool.parameters`` in
    place; return each param's disposition (``"injected"`` / ``"native"``),
    keyed independently — a tool that already declares one of the two names
    is left untouched for that name only.

    Unlike the FastMCP middleware — which deep-copies on every ``on_list_tools``
    — this runs once at install and mutates the tool's canonical schema dict
    directly, because that dict IS what ``FastMCP.list_tools`` advertises as
    ``inputSchema``. So the wrapper forwards the caller's own value for a
    ``"native"`` param instead of stripping it. Mirrors baton-extmcp's injector."""
    schema = tool.parameters
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    existing = props if isinstance(props, dict) else {}
    dispositions: dict[str, str] = {}
    to_inject: dict[str, dict[str, str]] = {}
    for name, build_desc in (
        (USER_GOAL_PARAM_NAME, build_user_goal_param_description),
        (EXPECTED_RESULT_PARAM_NAME, build_expected_result_param_description),
    ):
        if name in existing:
            dispositions[name] = "native"
        else:
            dispositions[name] = "injected"
            to_inject[name] = {"type": "string", "description": build_desc()}
    if not to_inject:
        return dispositions
    new_props = schema.setdefault("properties", {})
    if not isinstance(new_props, dict):
        return dispositions
    new_props.update(to_inject)
    if dispositions[USER_GOAL_PARAM_NAME] == "injected" and intent_param_mode == "required":
        required = schema.get("required")
        if isinstance(required, list):
            if USER_GOAL_PARAM_NAME not in required:
                required.append(USER_GOAL_PARAM_NAME)
        else:
            schema["required"] = [USER_GOAL_PARAM_NAME]
    return dispositions


def _extract_goal_params(
    tool_name: str,
    arguments: dict[str, Any],
    intent_param_mode: str,
    param_registry: dict[str, dict[str, str]],
) -> tuple[str | None, str | None]:
    """Pop the injected ``user_goal``/``expected_result`` from ``arguments`` in
    place; return their values independently (either may be absent).

    Mutating in place is what keeps them off the vendor handler — the same
    dict is forwarded to ``original_run``."""
    if intent_param_mode == "off":
        return None, None
    dispositions = param_registry.get(tool_name)
    goal = _extract_one_goal_param(tool_name, arguments, USER_GOAL_PARAM_NAME, dispositions)
    expected = _extract_one_goal_param(
        tool_name, arguments, EXPECTED_RESULT_PARAM_NAME, dispositions
    )
    return goal, expected


def _extract_one_goal_param(
    tool_name: str,
    arguments: dict[str, Any],
    param_name: str,
    dispositions: dict[str, str] | None,
) -> str | None:
    """Registry dispositions mirror baton-extmcp: ``"native"`` → the param is
    the vendor's, forward untouched; unknown (cold registry — a call arrived
    before the tool was scanned) → strip with a warning, safe only because the
    names are reserved. Never raises."""
    if param_name not in arguments:
        return None
    disposition = dispositions.get(param_name) if dispositions is not None else None
    if disposition == "native":
        return None
    if disposition is None:
        logger.warning(
            "baton: stripping %s from unlisted tool %r (cold registry)",
            param_name,
            tool_name,
        )
    raw = arguments.pop(param_name, None)
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
        [str, str, dict[str, Any], dict[str, Any] | None, str | None], Awaitable[None]
    ],
    emit_after: Callable[[str, str, Any, float, dict[str, Any] | None], Awaitable[None]],
    emit_error: Callable[[str, str, BaseException, float, dict[str, Any] | None], Awaitable[None]],
    emit_proactive: Callable[[str, str, str, str | None, dict[str, Any] | None], Awaitable[None]],
    scrubber: Callable[[Any], Any],
    *,
    intent_param_mode: str,
    param_registry: dict[str, dict[str, str]],
    tracker: ProactiveTracker,
    fallback_session_id: str,
    resolve_session_id_hook: ResolveSessionIdHook | None,
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
        # Strip the injected goal params IN PLACE, before snapshotting params —
        # ``arguments`` is the same object forwarded to the vendor handler, so
        # the strip keeps ``user_goal``/``expected_result`` off the tool AND out
        # of the captured ``params`` (which must equal the vendor-visible
        # arguments).
        call_intent: str | None = None
        call_expected: str | None = None
        if isinstance(arguments, dict):
            call_intent, call_expected = _extract_goal_params(
                name, arguments, intent_param_mode, param_registry
            )
        scrubbed_intent = scrubber(call_intent) if call_intent is not None else None
        scrubbed_expected = scrubber(call_expected) if call_expected is not None else None

        params = dict(arguments or {})
        meta_dict = _extract_meta_from_context(context)
        scrubbed_meta = scrubber(meta_dict) if meta_dict is not None else None
        call_session_id = await _resolve_call_session_id(
            context,
            meta_dict,
            fallback_session_id,
            resolve_hook=resolve_session_id_hook,
            tool_name=name,
            arguments=params,
        )

        # The session's FIRST injected intent also becomes a proactive
        # annotation (carrying expected_result too, if present), sequenced
        # BEFORE the tool_call_start it explains. ``claim`` dedups per session
        # and is suppressed when a real annotation-tool proactive already
        # fired. Later param intents ride only the start event.
        if scrubbed_intent is not None and tracker.claim(call_session_id):
            await emit_proactive(
                call_session_id, name, scrubbed_intent, scrubbed_expected, scrubbed_meta
            )

        # MRTR (mcp>=2.0): a continuation carries input_responses/request_state
        # from an earlier InputRequiredResult pause — it's the SAME logical call
        # resuming, not a new one, so it gets no new tool_call_start. The goal-
        # param strip above still runs unconditionally regardless: if a
        # continuation resends the original arguments, user_goal/expected_result
        # must still never reach the vendor handler.
        is_continuation = _is_mrtr_continuation(context)
        if not is_continuation:
            await emit_before(
                call_session_id, name, scrubber(params), scrubbed_meta, scrubbed_intent
            )
        called_at = monotonic()
        try:
            result = await original_run(arguments, context=context, convert_result=convert_result)
        except BaseException as exc:
            # mcp's Tool.run does `raise ToolError(...) from e` — surface the
            # original __cause__ when present so error_type reflects the real
            # exception class the vendor's fn actually raised.
            original_exc = exc.__cause__ if exc.__cause__ is not None else exc
            await emit_error(
                call_session_id, name, original_exc, monotonic() - called_at, scrubbed_meta
            )
            raise
        # MRTR (mcp>=2.0): an InputRequiredResult means the call paused mid-flight
        # to ask the client for more input — it hasn't finished, so no
        # tool_call_end. Whichever round eventually returns something else
        # (or errors) is the one that gets the real end/error event.
        if not _is_mrtr_pause(result):
            await emit_after(call_session_id, name, result, monotonic() - called_at, scrubbed_meta)
        return result

    setattr(wrapper, _WRAPPED_SENTINEL, True)
    return wrapper


def _trace_id_from_traceparent(traceparent: Any) -> str | None:
    """The trace-id field of a W3C ``traceparent`` value
    (``version-trace_id-parent_id-flags``, SEP-414) — SPEC §3.4 rung 1's
    preferred ``session_id`` source. ``None`` on any malformed or all-zero
    input; never raises."""
    if not isinstance(traceparent, str):
        return None
    parts = traceparent.split("-")
    if len(parts) != 4:
        return None
    trace_id = parts[1]
    if not trace_id or trace_id == "0" * len(trace_id):
        return None
    return trace_id


def _resolve_session_id_from_meta(meta: dict[str, Any] | None) -> str | None:
    """SPEC §3.4 rungs 1-2, in priority order: ``_meta.traceparent`` (W3C
    trace context, SEP-414) then ``_meta["io.baton/session_id"]``
    (vendor-supplied app-level handle). ``None`` if neither is present.

    Per SPEC §5.2's validated runtime table, no MCP client Baton has tested
    (Claude Code, Claude Desktop, Cursor) populates either key today — so in
    practice this misses for every currently-known runtime. Still worth
    reading: the data is already extracted for ``runtime_meta`` (free), and
    unlike the header rung below, neither key depends on which MCP protocol
    version was negotiated — this starts resolving automatically the moment
    any runtime adopts SEP-414 or a vendor's own first-party client stamps
    the Baton key, with no further SDK change.
    """
    if not meta:
        return None
    trace_id = _trace_id_from_traceparent(meta.get("traceparent"))
    if trace_id is not None:
        return trace_id
    app_handle = meta.get("io.baton/session_id")
    if isinstance(app_handle, str) and app_handle:
        return app_handle
    return None


def _extract_headers_from_context(context: Any) -> Mapping[str, str] | None:
    """Best-effort HTTP header extraction, shared by rung 0 (the vendor hook's
    ``SessionResolutionContext.headers``) and rung 4 below. ``None`` on stdio,
    outside a live request, or any attribute miss; never raises."""
    if context is None:
        return None
    try:
        headers = getattr(context, "headers", None)
        if not headers:
            rc = context.request_context
            request = getattr(rc, "request", None) if rc is not None else None
            headers = getattr(request, "headers", None) if request is not None else None
    except (AttributeError, ValueError):
        return None
    return headers if headers else None


async def _resolve_call_session_id(
    context: Any,
    meta: dict[str, Any] | None,
    fallback: str,
    *,
    resolve_hook: ResolveSessionIdHook | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Real per-call session id. Rung 0 (a configured
    ``VendorConfig.resolve_session_id`` hook) is checked first and, on a
    non-empty return, wins outright — see ``docs/design-notes/
    session_resolver_hook.md``. Below that, SPEC §3.4's layered fallback in
    priority order: (1) ``_meta.traceparent``, (2)
    ``_meta["io.baton/session_id"]``, (4) the ``mcp-session-id`` HTTP header,
    else (5) ``fallback`` (the install-time process-wide id). Rung 3 (a
    future runtime-specific ``_meta`` key) isn't defined for any runtime yet,
    so it's skipped.

    The header rung (4) is stateful-HTTP-only and protocol-version-sensitive:
    ``stateless_http`` defaults to ``False`` on both mcp 1.x and 2.0, so the
    header is present on old-spec streamable HTTP (the documented hosted
    shape — one process, many users). But MCP protocol 2026-07-28+ (SEP-2567)
    removes the header from the wire entirely when a client negotiates that
    version — confirmed in mcp 2.0.0's ``_streamable_http_modern.py`` ("no
    `Mcp-Session-Id`") — so on a new-spec connection this rung always misses
    regardless of vendor deployment shape, which is exactly why rungs 1-2 are
    checked first. On stdio there's no HTTP request, so the header rung
    always misses and ``fallback`` is correct there (one process = one
    user). On stateless HTTP (``stateless_http=True``, opt-in, no current
    vendor) there's no header by protocol design either — that miss isn't a
    bug this function can fix; see ``project_sdk_sensor_parity_gap`` memory
    for why that needs a vendor-configurable resolver instead.

    mcp 2.0's ``Context`` exposes ``.headers`` directly; mcp 1.x has no such
    accessor, so ``_extract_headers_from_context`` also tries reaching
    through ``request_context.request.headers`` (a raw transport request
    object on HTTP transports, ``None`` on stdio). Never raises —
    best-effort like ``_extract_meta_from_context`` below, including outside
    a live request (``request_context`` raises ``ValueError`` there on both
    SDK versions).
    """
    headers = _extract_headers_from_context(context)
    if resolve_hook is not None:
        hook_result = await resolve_via_hook(
            resolve_hook,
            SessionResolutionContext(
                headers=headers, meta=meta, tool_name=tool_name, arguments=arguments
            ),
        )
        if hook_result is not None:
            return hook_result
    from_meta = _resolve_session_id_from_meta(meta)
    if from_meta is not None:
        return from_meta
    if headers is None:
        return fallback
    session_id = headers.get("mcp-session-id")
    return session_id if isinstance(session_id, str) and session_id else fallback


def _is_mrtr_continuation(context: Any) -> bool:
    """True if this ``Tool.run`` invocation is a continuation of a previously
    paused multi-round-trip (MRTR) call — mcp>=2.0's ``Context.input_responses``/
    ``request_state`` carry the client's answers to an earlier
    ``InputRequiredResult``'s ``input_requests`` (SEP, 2026-07-28+). Duck-typed,
    not an ``isinstance`` check against ``mcp_types`` — mcp<2.0 has no such
    properties on ``Context`` at all, so this is always False there. Never
    raises: mirrors the rest of this module's best-effort context reads."""
    if context is None:
        return False
    try:
        return (
            getattr(context, "input_responses", None) is not None
            or getattr(context, "request_state", None) is not None
        )
    except (AttributeError, ValueError):
        return False


def _is_mrtr_pause(result: Any) -> bool:
    """True if ``result`` is an mcp>=2.0 ``InputRequiredResult`` — the tool call
    paused mid-flight to ask the client for more input rather than completing.
    Duck-typed on the wire discriminator (``result_type == "input_required"``,
    the tag ``FuncMetadata.convert_result`` passes an ``InputRequiredResult``
    through unchanged to preserve) rather than importing ``mcp_types`` — mcp<2.0
    has no such type, and every other completed-result shape here
    (``CallToolResult``, the 1.x tuple, a raw dict/model) either lacks
    ``result_type`` or carries ``"complete"``, never ``"input_required"``."""
    return getattr(result, "result_type", None) == "input_required"


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
    default_agent_runtime: str,
    scrubber: Callable[[Any], Any],
) -> tuple[
    Callable[[str, str, dict[str, Any], dict[str, Any] | None, str | None], Awaitable[None]],
    Callable[[str, str, Any, float, dict[str, Any] | None], Awaitable[None]],
    Callable[[str, str, BaseException, float, dict[str, Any] | None], Awaitable[None]],
    Callable[[str, str, str, str | None, dict[str, Any] | None], Awaitable[None]],
]:
    """Build four async emitters: ``tool_call_start`` / ``_end`` / ``_error``
    plus the synthesised-proactive ``annotation``.

    Each takes the per-call ``session_id`` resolved by
    ``_resolve_call_session_id`` as its first argument — real on stateful
    HTTP, ``fallback_session_id`` otherwise (stdio, or no header found). The
    Console worker also uses ``runtime_meta`` (populated below) for finer
    per-cycle correlation per SPEC §11.5, independent of this.
    """

    async def _seq(session_id: str) -> int:
        return await counter.next(session_id)

    async def emit_proactive(
        session_id: str,
        name: str,
        intent: str,
        expected_outcome: str | None,
        runtime_meta: dict[str, Any] | None,
    ) -> None:
        await safe_write(
            sink,
            AnnotationEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=await _seq(session_id),
                captured_at=datetime.now(UTC),
                agent_runtime=default_agent_runtime,
                runtime_meta=runtime_meta,
                payload=AnnotationPayload(
                    intent=intent,
                    expected_outcome=expected_outcome,
                    intent_source=INTENT_SOURCE_PARAM,
                    tool_name=name,
                ),
            ),
            logger,
        )

    async def emit_before(
        session_id: str,
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
                session_id=session_id,
                sequence_number=await _seq(session_id),
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
        session_id: str,
        name: str,
        result: Any,
        duration_s: float,
        runtime_meta: dict[str, Any] | None,
    ) -> None:
        await safe_write(
            sink,
            ToolCallEndEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=await _seq(session_id),
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
        session_id: str,
        name: str,
        exc: BaseException,
        duration_s: float,
        runtime_meta: dict[str, Any] | None,
    ) -> None:
        await safe_write(
            sink,
            ToolCallErrorEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=await _seq(session_id),
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

    ``Tool.run`` is called with ``convert_result=True`` (which mcp's
    ``call_tool`` does), and the wire envelope it returns changed shape across
    the mcp 1.x → 2.0 rename:

    - **mcp 1.x** returns the tuple ``(content_list, structured_result_dict)``
      where ``structured_result_dict`` typically looks like
      ``{"result": <the fn's return value>}``.
    - **mcp 2.0** returns a ``CallToolResult`` object exposing the same values
      as ``.content`` / ``.structured_content``.

    Either way we unwrap the developer-meaningful return so
    ``tool_call_end.result`` captures it, not the MCP wire envelope.
    """
    if result is None:
        return None
    # mcp 1.x: (content, structured_result) tuple from convert_result=True.
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        if isinstance(structured, dict) and "result" in structured:
            return _result_to_jsonable(structured["result"])
        # Fallback: serialize the content list.
        return _result_to_jsonable(content)
    # mcp 2.0: CallToolResult object from convert_result=True. ``structured_content``
    # is the marker attribute; unwrap ``{"result": ...}`` like the 1.x tuple, else
    # fall back to the content list before the generic model_dump below.
    if hasattr(result, "structured_content"):
        structured = result.structured_content
        if isinstance(structured, dict) and "result" in structured:
            return _result_to_jsonable(structured["result"])
        content = getattr(result, "content", None)
        if content is not None:
            return _result_to_jsonable(content)
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    if isinstance(result, (str, int, float, bool, list, dict)):
        return result
    return str(result)
