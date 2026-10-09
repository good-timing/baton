"""Tool-handler wrapping — the capture mechanism for the official mcp SDK.

``mcp.server.fastmcp.FastMCP`` exposes no middleware hook. Instead, we wrap
each registered ``Tool.run`` method in place. Per the spike, this is stable
across mcp v1.10 → v1.27 (verified by the CI matrix).

Why wrap ``Tool.run`` (instead of ``Tool.fn`` as the 0.2.x adapter did):
- We receive the raw ``arguments`` dict directly — no inspect.signature
  binding gymnastics to map positional args back to parameter names.
- We receive the request ``context`` directly — that's where the MCP
  ``_meta`` lives, which we forward as the event envelope's ``runtime_meta``
  field per SPEC §11.4.1 (the primitive the Console worker uses for turn
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
``tool_call_end`` on success, ``tool_call_error`` on FAILURE — which MCP
expresses two ways (SPEC §11.4.3): the handler raises, or it returns a result
carrying MCP's error flag on a 200. Reading only the first is what this
adapter did, and what §6.1's own wording told it to. Re-raises
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

import copy
import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from baton._meta_coords import round_meta_coordinates
from baton._result_capture import ResultCaptureMode, end_result_fields
from baton._state import ProactiveTracker, SessionCounter
from baton._uuid import uuid7
from baton.events import (
    AnnotationEvent,
    AnnotationPayload,
    ClientObserved,
    PrincipalWire,
    SurfaceSnapshotEvent,
    SurfaceSnapshotPayload,
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallErrorEvent,
    ToolCallErrorPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from baton.integrations._config import (
    SessionResolutionContext,
)
from baton.integrations._error_result import (
    TOOL_ERROR_TYPE,
    ErrorResultFields,
    is_error_result,
    returned_error_fields,
)
from baton.integrations._llm_text import (
    EXPECTED_RESULT_PARAM_NAME,
    INTENT_SOURCE_PARAM,
    OVERALL_TASK_PARAM_NAME,
    USER_GOAL_PARAM_NAME,
    build_expected_result_param_description,
    build_overall_task_param_description,
    build_user_goal_param_description,
    drop_subagent_clause,
    required_param_names,
)
from baton.integrations._session import (
    session_id_from_headers,
)
from baton.integrations._surface import assemble_surface, build_seam_augmentations, surface_hash
from baton.integrations.client_observed import meta_to_dict, observe_client
from baton.integrations.identity_adapter import (
    ResolvePrincipalHook,
    resolve_call_principal,
    token_claims,
)
from baton.integrations.official import _auth
from baton.integrations.official._compat import set_server_instructions
from baton.integrations.official._registry import get_tool_manager, get_tool_registry
from baton.scrub import identity_scrub, scrub_or_none
from baton.sinks import Sink, safe_emit

logger = logging.getLogger(__name__)

# Sentinel attribute set on wrapped run methods so repeated re-scans don't
# double-wrap. Tools added via the patched add_tool are checked against
# this before wrapping.
_WRAPPED_SENTINEL = "_baton_wrapped"


class _SurfaceState:
    """Tracks the vendor-true (pre-injection) tool surface for this install,
    for ``surface_snapshot`` capture. Unlike the FastMCP middleware — which
    sees each listing's tools before it injects into them — this adapter
    injects into the registered schema in place, so a listing only ever
    shows the injected shape. The vendor-true one is built from data already
    in hand at install/add_tool time and lazily hashed+emitted on the next
    tool call, the first point execution is guaranteed to be inside an async
    context.

    ``raw_tools`` is keyed by tool name, capturing each tool's wire shape
    BEFORE ``_inject_goal_params`` mutates ``tool.parameters`` in place —
    the annotation tool is never added here (``_maybe_wrap_entry`` returns
    before this runs for it), matching proxy's split of vendor tools vs.
    ``seam_augmentations.injected_tools``.
    """

    def __init__(self, server_meta: dict[str, Any]) -> None:
        self.server_meta = server_meta
        self.raw_tools: dict[str, dict[str, Any]] = {}
        self.emitted_hashes: set[str] = set()
        self.dirty = False

    def note_tool(self, name: str, tool: Any) -> None:
        # Deep-copy: ``tool.parameters`` is the SAME dict object
        # ``_inject_goal_params`` mutates in place immediately after this
        # call — a reference here would let that mutation silently corrupt
        # the vendor-true snapshot too.
        schema = tool.parameters
        self.raw_tools[name] = {
            "name": name,
            "description": tool.description,
            "inputSchema": copy.deepcopy(schema) if isinstance(schema, dict) else {},
        }
        self.dirty = True

    def build_snapshot(self) -> dict[str, Any]:
        return assemble_surface(
            self.server_meta, [self.raw_tools[name] for name in sorted(self.raw_tools)]
        )


def install_wraps(
    mcp: Any,
    *,
    tenant_id: str,
    vendor_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    fallback_session_id: str,
    scrubber: Callable[[Any], Any] = identity_scrub,
    annotation_tool_name: str | None = None,
    intent_param_mode: str = "required",
    result_capture_mode: ResultCaptureMode = "full",
    proactive_tracker: ProactiveTracker | None = None,
    server_meta: dict[str, Any] | None = None,
    resolve_principal_hook: ResolvePrincipalHook | None = None,
) -> None:
    """Inject + wrap all currently-registered tools AND future registrations."""
    tracker = proactive_tracker or ProactiveTracker()
    # tool_name -> {param_name: "injected" | "native"}. Populated as tools are
    # injected; read in the wrapper to decide strip-vs-forward, per param,
    # independently. A plain dict (no lock) is safe: all access is on the one
    # asyncio loop, so no statement interleaves.
    param_registry: dict[str, dict[str, str]] = {}
    surface_state = _SurfaceState(server_meta or {})
    emit_before, emit_after, emit_error, emit_proactive, emit_surface = _make_emitters(
        tenant_id=tenant_id,
        vendor_id=vendor_id,
        consent_token=consent_token,
        sink=sink,
        counter=counter,
        scrubber=scrubber,
        result_capture_mode=result_capture_mode,
    )

    def _maybe_wrap_entry(name: str, tool: Any) -> None:
        # Skip the annotation tool — its handler emits its own annotation
        # event with the structured payload, and it takes ``intent`` explicitly
        # so it needs no injected goal params.
        if annotation_tool_name is not None and name == annotation_tool_name:
            return
        # A tool whose `run` already carries our sentinel is THIS EXACT
        # object, already fully processed. add_tool_with_wrap re-walks the
        # FULL registry on every add_tool call, so this check must come
        # first and gate everything below it — re-running note_tool on an
        # already-wrapped tool would capture its schema AFTER injection
        # mutated `tool.parameters` in place, corrupting the vendor-true
        # snapshot. A tool object without the sentinel is either brand new
        # or a genuine re-registration under an existing name (a fresh
        # ``Tool`` instance replacing the old one, e.g. a vendor's hot
        # reload) — either way its schema is pre-injection and must be
        # (re-)captured, which is why this is a re-registration check, not
        # just a first-time-install check.
        if getattr(tool.run, _WRAPPED_SENTINEL, False):
            return
        # Captured BEFORE injection mutates ``tool.parameters`` in place, so
        # the surface snapshot reflects the vendor's real advertised schema.
        # Unconditional here (no `name not in raw_tools` guard) — see above.
        surface_state.note_tool(name, tool)
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
                if dispositions.get(OVERALL_TASK_PARAM_NAME) == "native":
                    drop_subagent_clause(mcp, name, set_server_instructions)
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
                result_capture_mode=result_capture_mode,
                param_registry=param_registry,
                tracker=tracker,
                fallback_session_id=fallback_session_id,
                tenant_id=tenant_id,
                resolve_principal_hook=resolve_principal_hook,
                surface_state=surface_state,
                emit_surface=emit_surface,
                annotation_tool_name=annotation_tool_name,
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

    # 3. Patch remove_tool so a runtime removal prunes the surface snapshot
    # too — otherwise raw_tools (append-only, keyed by name) keeps reporting
    # a phantom tool the live tools/list no longer advertises.
    original_remove_tool = manager.remove_tool

    @functools.wraps(original_remove_tool)
    def remove_tool_with_prune(name: str, *args: Any, **kwargs: Any) -> Any:
        result = original_remove_tool(name, *args, **kwargs)
        if surface_state.raw_tools.pop(name, None) is not None:
            surface_state.dirty = True
        return result

    manager.remove_tool = remove_tool_with_prune


# =============================================================================
# Internals — intent-param injection + strip
# =============================================================================


def _inject_goal_params(tool: Any, intent_param_mode: str) -> dict[str, str]:
    """Inject the three intent params into ``tool.parameters`` in
    place; return each param's disposition (``"injected"`` / ``"native"``),
    keyed independently — a tool that already declares one of the names
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
    for name, description in (
        (
            USER_GOAL_PARAM_NAME,
            build_user_goal_param_description(intent_param_mode=intent_param_mode),
        ),
        (
            EXPECTED_RESULT_PARAM_NAME,
            build_expected_result_param_description(intent_param_mode=intent_param_mode),
        ),
        (
            OVERALL_TASK_PARAM_NAME,
            build_overall_task_param_description(intent_param_mode=intent_param_mode),
        ),
    ):
        if name in existing:
            dispositions[name] = "native"
        else:
            dispositions[name] = "injected"
            to_inject[name] = {"type": "string", "description": description}
    if not to_inject:
        return dispositions
    new_props = schema.setdefault("properties", {})
    if not isinstance(new_props, dict):
        return dispositions
    new_props.update(to_inject)
    # Only params this adapter actually injected are escalated — a tool that
    # declares one of the names natively keeps its own, per-param.
    escalated = [
        name
        for name in required_param_names(intent_param_mode=intent_param_mode)
        if dispositions.get(name) == "injected"
    ]
    if escalated:
        required = schema.get("required")
        if isinstance(required, list):
            for name in escalated:
                if name not in required:
                    required.append(name)
        else:
            schema["required"] = list(escalated)
    return dispositions


def _extract_goal_params(
    tool_name: str,
    arguments: dict[str, Any],
    intent_param_mode: str,
    param_registry: dict[str, dict[str, str]],
) -> tuple[str | None, str | None, str | None]:
    """Pop the injected ``user_goal``/``expected_result``/``overall_task``
    from ``arguments`` in place; return their values independently (any may
    be absent).

    Mutating in place is what keeps them off the vendor handler — the same
    dict is forwarded to ``original_run``."""
    if intent_param_mode == "off":
        return None, None, None
    dispositions = param_registry.get(tool_name)
    goal = _extract_one_goal_param(tool_name, arguments, USER_GOAL_PARAM_NAME, dispositions)
    expected = _extract_one_goal_param(
        tool_name, arguments, EXPECTED_RESULT_PARAM_NAME, dispositions
    )
    task = _extract_one_goal_param(tool_name, arguments, OVERALL_TASK_PARAM_NAME, dispositions)
    return goal, expected, task


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

# The five emitter signatures, written ONCE and referenced at both sites —
# the parameter list of ``_wrap_tool_run`` and the return tuple of
# ``_make_emitters``, which were previously two full copies each.
#
# Parameter ORDER is the contract. ``_make_emitters``' concrete defs name the
# same slots as keyword parameters, which is what lets mypy check the two
# spellings against each other; the slot comments below are what let a reader
# do the same without running it.
_EmitBefore = Callable[
    [
        str,  # session_id
        str,  # tool name
        dict[str, Any],  # params
        dict[str, Any] | None,  # runtime_meta
        str | None,  # call_intent
        str | None,  # call_expected
        str | None,  # call_workflow
        ClientObserved | None,  # client_observed
        PrincipalWire | None,  # principal
        str,  # call_id
        str | None,  # transport_observed
    ],
    Awaitable[None],
]

_EmitAfter = Callable[
    [
        str,  # session_id
        str,  # tool name
        Any,  # result
        float,  # duration_s
        dict[str, Any] | None,  # runtime_meta
        ClientObserved | None,  # client_observed
        PrincipalWire | None,  # principal
        str,  # call_id
        str | None,  # transport_observed
    ],
    Awaitable[None],
]

# ⚠ ``result`` is positional and has NO default, though the emitter could
# carry one. A default would let the raise site inherit ``None`` silently; as
# written, each of the two failure shapes (SPEC §11.4.3) has to say which it
# is. ``exc`` is gone from this signature for the same reason the event stopped
# meaning "the handler raised": only the caller knows whether it holds a live
# exception or a returned result whose error flag is set.
_EmitError = Callable[
    [
        str,  # session_id
        str,  # tool name
        str,  # error_type
        float,  # duration_s
        dict[str, Any] | None,  # runtime_meta
        ClientObserved | None,  # client_observed
        PrincipalWire | None,  # principal
        str,  # call_id
        str | None,  # transport_observed
        # ⚠ The RAW inputs to the result-derived members, never the members
        # themselves. Resolving them CALLS the vendor's scrubber, and an
        # argument expression is evaluated in the caller's frame — outside the
        # `safe_emit` thunk meant to guard it. Passing a finished
        # `ErrorResultFields`, or a thunk returning one, both type-check
        # whether or not the scrubber already ran, so the discipline lived in a
        # comment; passing the exception and the result makes it structural.
        # `emit_after` takes its `result` raw for the same reason.
        #
        # Exactly one is set, and that is the RAISE/RETURN discriminator
        # (SPEC §11.4.3): an exception means the handler raised and there is no
        # result object to record; a result means it returned with the error
        # flag set.
        BaseException | None,  # raised
        Any,  # result
    ],
    Awaitable[None],
]

_EmitProactive = Callable[
    [
        str,  # session_id
        str,  # tool name
        str,  # intent
        str | None,  # expected_outcome
        str | None,  # workflow
        dict[str, Any] | None,  # runtime_meta
        ClientObserved | None,  # client_observed
        PrincipalWire | None,  # principal
        str | None,  # transport_observed
    ],
    Awaitable[None],
]

_EmitSurface = Callable[[str, str, dict[str, Any]], Awaitable[None]]


def _wrap_tool_run(
    name: str,
    tool: Any,
    emit_before: _EmitBefore,
    emit_after: _EmitAfter,
    emit_error: _EmitError,
    emit_proactive: _EmitProactive,
    scrubber: Callable[[Any], Any],
    *,
    intent_param_mode: str,
    result_capture_mode: ResultCaptureMode,
    param_registry: dict[str, dict[str, str]],
    tracker: ProactiveTracker,
    fallback_session_id: str,
    tenant_id: str,
    resolve_principal_hook: ResolvePrincipalHook | None,
    surface_state: _SurfaceState,
    emit_surface: _EmitSurface,
    annotation_tool_name: str | None,
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
        # Surface snapshot — lazy (see _SurfaceState docstring): this is the
        # first point every install is guaranteed to reach an async context. ``dirty``
        # keeps this a no-op on every call after the first stable surface.
        # Fail-open, mirroring the fastmcp adapter's on_list_tools capture —
        # this must never block the vendor's tool call. ``dirty`` is cleared
        # FIRST so a hashing/serialization error (deterministic — would just
        # re-throw identically every call) is one attempt per surface
        # change, not a retry storm. A WRITE failure is different — sink
        # health can recover — so that path re-sets ``dirty = True`` to
        # retry on the next call, and ``emitted_hashes`` is only updated
        # AFTER a successful write (a transient failure must not
        # permanently drop this surface, unlike a genuine dedup skip).
        if surface_state.dirty:
            surface_state.dirty = False
            try:
                snapshot = surface_state.build_snapshot()
                digest = surface_hash(snapshot)
            except Exception:
                logger.exception("baton: surface snapshot capture failed")
            else:
                if digest not in surface_state.emitted_hashes:
                    seam = build_seam_augmentations(
                        injected_tool_names=(
                            [annotation_tool_name] if annotation_tool_name else []
                        ),
                        intent_param_names=[
                            USER_GOAL_PARAM_NAME,
                            EXPECTED_RESULT_PARAM_NAME,
                            OVERALL_TASK_PARAM_NAME,
                        ],
                        intent_param_mode=intent_param_mode,
                        required_names=list(
                            required_param_names(intent_param_mode=intent_param_mode)
                        ),
                    )
                    try:
                        await emit_surface(
                            fallback_session_id, digest, {**snapshot, "seam_augmentations": seam}
                        )
                    except Exception:
                        logger.exception("baton: surface snapshot capture failed")
                        surface_state.dirty = True
                    else:
                        surface_state.emitted_hashes.add(digest)

        # Strip the injected goal params IN PLACE, before snapshotting params —
        # ``arguments`` is the same object forwarded to the vendor handler, so
        # the strip keeps ``user_goal``/``expected_result`` off the tool AND out
        # of the captured ``params`` (which must equal the vendor-visible
        # arguments).
        call_intent: str | None = None
        call_expected: str | None = None
        call_task: str | None = None
        if isinstance(arguments, dict):
            call_intent, call_expected, call_task = _extract_goal_params(
                name, arguments, intent_param_mode, param_registry
            )
        # scrub_or_none: OUTSIDE any `safe_emit` build thunk, so a raising
        # vendor scrubber here would break the vendor's tool call (SPEC §11.2).
        scrubbed_intent = scrub_or_none(scrubber, call_intent, USER_GOAL_PARAM_NAME, logger)
        scrubbed_expected = scrub_or_none(
            scrubber, call_expected, EXPECTED_RESULT_PARAM_NAME, logger
        )
        scrubbed_task = scrub_or_none(scrubber, call_task, OVERALL_TASK_PARAM_NAME, logger)

        params = dict(arguments or {})
        meta_dict = _extract_meta_from_context(context)
        # Identity resolves HERE, in one place per call, before anything is
        # emitted.
        # ``resolve_call_principal`` returns the FINISHED wire value — a hash or
        # a deliberate raw principal — so the raw identity never travels past
        # this line into the emitters, mirroring baton-proxy's edge-hash
        # chokepoint. ``None`` when no hook is set or it has no answer.
        #
        # ⚠ This comment used to read "Unauthenticated calls (every stdio one)
        # get ``None``". That stopped being true when ``resolve_principal`` landed:
        # the hook is the only identity mechanism stdio has, and carrying a
        # stdio principal is the reason it was built.
        # One read per call, shared by ``client_observed``, the identity hook
        # and the session ladder.
        call_headers = _extract_headers_from_context(context)
        # From the raw meta: the vendor's scrubber is applied to each value
        # inside, so one that removes meta keys cannot hide the declaration.
        call_client = observe_client(
            meta_dict, context=context, headers=call_headers, scrubber=scrubber
        )
        # Read from the same context, one line apart, and DELIBERATELY not from
        # ``call_headers`` above: that helper folds an AttributeError into the
        # same ``None`` as a real absence (register A6), and this field exists
        # to keep those apart. See ``observe_transport``.
        call_transport = observe_transport(context)
        identity_hook_context = (
            SessionResolutionContext(
                headers=call_headers,
                meta=meta_dict,
                tool_name=name,
                arguments=params,
                claims=token_claims(_auth.current_access_token()),
            )
            if resolve_principal_hook is not None
            else None
        )
        call_principal = await resolve_call_principal(
            hook=resolve_principal_hook,
            hook_context=identity_hook_context,
            logger=logger,
        )
        # Coordinates round BEFORE the vendor's scrubber, so they round whatever
        # scrubber is configured (``_meta_coords``).
        scrubbed_meta = scrub_or_none(
            scrubber,
            round_meta_coordinates(meta_dict) if meta_dict is not None else None,
            "_meta",
            logger,
        )
        call_session_id = await _resolve_call_session_id(
            headers=call_headers, fallback=fallback_session_id
        )

        # The session's FIRST injected intent also becomes a proactive
        # annotation (carrying expected_result too, if present), sequenced
        # BEFORE the tool_call_start it explains. ``claim`` dedups per session
        # and is suppressed when a real annotation-tool proactive already
        # fired. Later param intents ride only the start event.
        if scrubbed_intent is not None and tracker.claim(call_session_id):
            await emit_proactive(
                call_session_id,
                name,
                scrubbed_intent,
                scrubbed_expected,
                scrubbed_task,
                scrubbed_meta,
                call_client,
                call_principal,
                call_transport,
            )

        # The per-call join key (SPEC §11.4). A LOCAL of this wrapper call,
        # which is the scope that emits both legs, so it is per-call by
        # construction and correct across processes. Hoisting it — onto the
        # closure, the tool, or the module — would send one id for a whole
        # session and degrade tier 1 to the FIFO floor it outranks, invisibly
        # (a mispair is a permutation, so every total holds). Never
        # ``ctx.request_id``: it restarts at 1 per connection, so it collides
        # under exactly the merged-session conditions where pairing already
        # hurts.
        #
        # ⚠ An MRTR call spans TWO wrapper invocations, so its start (round 1)
        # and its end (round 2) get DIFFERENT ids and pair with nothing. That
        # is deliberate and is the console's live behaviour today: the tiers
        # never mix, so both legs stay honestly unpaired rather than one
        # stealing a neighbour's. It is also not fixable here — the two rounds
        # of a distributed MRTR call reach different processes with different
        # fallback session ids, so they never reach the pairer in one list at
        # all. Tracked internally; nothing here can close it.
        call_id = str(uuid7())

        # MRTR (mcp>=2.0): a continuation carries input_responses/request_state
        # from an earlier InputRequiredResult pause — it's the SAME logical call
        # resuming, not a new one, so it gets no new tool_call_start. The goal-
        # param strip above still runs unconditionally regardless: if a
        # continuation resends the original arguments, user_goal/expected_result
        # must still never reach the vendor handler.
        is_continuation = _is_mrtr_continuation(context)
        if not is_continuation:
            # ⚠ scrub_or_none, not a bare call. ``emit_before`` builds inside
            # a ``safe_emit`` thunk — but THIS runs while its ARGUMENTS are
            # evaluated, before that guard is entered, which is the same hole
            # ``safe_emit`` was added to close one level down. Missed when the
            # official adapter was converted (`eb4fb8f`) and found by teaching
            # the AST sweep its own guard shapes.
            #
            # ⚠ ``{}`` is a LOSSY degradation: a consumer reads it as "called
            # with no arguments", not "we could not scrub them". Accepted for
            # the reason ``client.py:with_params`` records — the only way to
            # assert nothing is to drop the START event, which orphans the end
            # and loses the whole call.
            await emit_before(
                call_session_id,
                name,
                scrub_or_none(scrubber, params, "params", logger) or {},
                scrubbed_meta,
                scrubbed_intent,
                scrubbed_expected,
                scrubbed_task,
                call_client,
                call_principal,
                call_id,
                call_transport,
            )
        called_at = monotonic()
        try:
            result = await original_run(arguments, context=context, convert_result=convert_result)
        except BaseException as exc:
            # mcp's Tool.run does `raise ToolError(...) from e` — surface the
            # original __cause__ when present so error_type reflects the real
            # exception class the vendor's fn actually raised.
            original_exc = exc.__cause__ if exc.__cause__ is not None else exc
            # ⚠ The exception goes in RAW. Its scrub used to be an argument
            # expression, evaluated in THIS frame before ``emit_error`` was
            # entered, and this sits in an ``except`` with a ``raise`` under
            # it — so a throwing scrubber REPLACED the vendor's own exception
            # with ours. Same shape proven on the library path
            # (`spikes/client_failopen_1003/`); missed here when the official
            # adapter was converted (`eb4fb8f`), then guarded by a caller-side
            # try/except (`cab0143`), and now it cannot recur: the emitter owns
            # the projection, so there is no expression here to get wrong.
            # ``_seq`` is the one await left outside the thunk and cannot raise
            # (`_state.py`: a dict op under a lock), so the caller-side guard
            # went with the change.
            await emit_error(
                call_session_id,
                name,
                type(original_exc).__name__,
                monotonic() - called_at,
                scrubbed_meta,
                call_client,
                call_principal,
                call_id,
                call_transport,
                original_exc,
                None,
            )
            raise
        # MRTR (mcp>=2.0): an InputRequiredResult means the call paused mid-flight
        # to ask the client for more input — it hasn't finished, so no
        # tool_call_end. Whichever round eventually returns something else
        # (or errors) is the one that gets the real end/error event.
        if not _is_mrtr_pause(result):
            # SPEC §11.4.3: a returned result carrying MCP's error flag is a
            # FAILURE — MCP files it as a 200, so it arrives here rather than
            # through the except above. Checked AFTER the MRTR pause: a paused
            # round has not finished, so it is neither an end nor an error yet.
            if is_error_result(result):
                # ``TOOL_ERROR_TYPE`` is not result-derived, so the
                # classification is passed regardless: the call still failed
                # (SPEC §11.2.6). Everything that IS result-derived the
                # projection decides together, and it owns the scrubber call.
                await emit_error(
                    call_session_id,
                    name,
                    TOOL_ERROR_TYPE,
                    monotonic() - called_at,
                    scrubbed_meta,
                    call_client,
                    call_principal,
                    call_id,
                    call_transport,
                    None,
                    result,
                )
            else:
                await emit_after(
                    call_session_id,
                    name,
                    result,
                    monotonic() - called_at,
                    scrubbed_meta,
                    call_client,
                    call_principal,
                    call_id,
                    call_transport,
                )
        return result

    setattr(wrapper, _WRAPPED_SENTINEL, True)
    return wrapper


def observe_transport(context: Any) -> str | None:
    """What we observed beneath this call, for the envelope's ``transport_observed``.

    ⚠ **Deliberately NOT built on ``_extract_headers_from_context`` below.** That
    helper catches ``AttributeError`` and returns the same ``None`` as a genuine
    absence, so an unexpected context shape reads there as "no HTTP" — register
    A6, a live defect. Deriving a transport from it would publish that bug as a
    fact about the customer's deployment, and ``no-http-request`` is not a label
    but a LICENCE: SPEC §3.4 lets a consumer group a process-wide fallback
    ``session_id`` on it and only on it. Handing that out because our own read
    crashed is how a producer bug becomes two strangers in one conversation.

    Nor does it consult ``context.headers``, which looks like a shortcut and is
    two traps. It is mcp 2.0+ only, so it is vacuously empty across the whole
    mcp 1.x band (register A7) — two of four supported legs — while the request
    object splits correctly on those same rows. And on mcp 2.x it is *derived
    from* ``request_context.request.headers``, so it adds no information and one
    more way to disagree.

    The five outcomes, keyed on the request object exactly as SPEC §11.4 requires:

    - no context at all → ``None``. We were handed nothing to look at.
    - ``request_context`` is itself ``None`` → ``None``. Same answer and same
      reason as the line above. Spelled out because omitting it is how it went
      unpinned: this list said "four outcomes" over five bullets until
      2026-09-22, and a test written from it inherited the miscount.
    - ``request_context`` raises ``ValueError`` → ``None``. The library's
      deliberate, documented answer to "is there a live request?" when a tool is
      called programmatically (``mcp.call_tool()``). **Not** ``no-http-request``,
      which SPEC defines as "a LIVE MCP request with no HTTP request behind it"
      — this is not that, and claiming it would assert a fact about a deployment
      that is not running. **Not** ``read-failed`` either: nothing failed.
    - ``rc.request is None`` → ``"no-http-request"``. Stdio or in-memory, where
      the attribute is real and its value is the signal (verified on mcp 2.1.1:
      ``ServerRequestContext.request: RequestT | None = None``).
    - a request object → ``"http"``.
    - anything else raised → ``"read-failed"``. The whole point of the value.
    """
    if context is None:
        return None
    try:
        rc = context.request_context
    except ValueError:
        return None
    except Exception:
        logger.debug("transport_observed: the request-context read raised", exc_info=True)
        return "read-failed"
    try:
        if rc is None:
            return None
        return "no-http-request" if rc.request is None else "http"
    except Exception:
        logger.debug("transport_observed: the request read raised", exc_info=True)
        return "read-failed"


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
    *,
    headers: Mapping[str, str] | None,
    fallback: str,
) -> str:
    """Real per-call session id — SPEC §3.4's layered fallback in priority
    order: (4) the ``mcp-session-id`` HTTP header, else (5) ``fallback`` (the
    install-time process-wide id). Rung 3 (a future runtime-specific ``_meta``
    key) isn't defined for any runtime yet, so it's skipped.

    **Rung 0 — ``VendorConfig.resolve_session_id`` — was REMOVED 2026-09-12**,
    for the reason that retired rungs 1-2: it keyed the session on an
    identifier the SDK did not mint. A vendor's handle differed from a
    client's only in who supplied it, which the join rule does not
    distinguish. What a vendor knows about a caller now reaches Baton through
    ``VendorConfig.resolve_principal`` and lands in ``principal``, where the console
    can group on it downstream and change its mind later.

    **Rungs 1-2 were retired 2026-09-09** — they keyed the session on
    identifiers the SDK did not mint (``_meta.traceparent``'s trace-id and a
    client-supplied ``_meta["io.baton/session_id"]``), which the D2 join rule
    forbids. Both values are still captured — ``runtime_meta`` forwards the
    whole ``_meta`` — so grouping on them is a downstream decision now. See
    ``integrations._session``.

    The header rung (4) is stateful-HTTP-only and protocol-version-sensitive:
    ``stateless_http`` defaults to ``False`` on both mcp 1.x and 2.0, so the
    header is present on old-spec streamable HTTP (the documented hosted
    shape — one process, many users). But MCP protocol 2026-07-28+ (SEP-2567)
    removes the header from the wire entirely when a client negotiates that
    version — confirmed in mcp 2.0.0's ``_streamable_http_modern.py`` ("no
    `Mcp-Session-Id`") — so on a new-spec connection this rung always misses
    regardless of vendor deployment shape, which is why the ladder
    terminates on ``fallback`` for that shape rather than on a meta rung. On
    stdio there's no HTTP request, so the header rung
    always misses and ``fallback`` is correct there (one process = one
    user). On stateless HTTP (``stateless_http=True``, opt-in, no current
    vendor) there's no header by protocol design either — that miss isn't a
    bug this function can fix. ⚠ **It also no longer has a workaround.** The
    vendor-configurable resolver that covered this shape was rung 0, removed
    2026-09-12, and SPEC rung 5 (a per-event UUID) is still unbuilt — so
    new-spec and stateless HTTP terminate on the process-wide ``fallback``,
    which is stable but merges every client of a multi-user server. That is
    D2/B4, and the answer is the console-side partition (N3), not a rung.

    mcp 2.0's ``Context`` exposes ``.headers`` directly; mcp 1.x has no such
    accessor, so ``_extract_headers_from_context`` also tries reaching
    through ``request_context.request.headers`` (a raw transport request
    object on HTTP transports, ``None`` on stdio). Never raises —
    best-effort like ``_extract_meta_from_context`` below, including outside
    a live request (``request_context`` raises ``ValueError`` there on both
    SDK versions).
    """
    # Extracted ONCE by the caller and passed in, never re-read here. It is a
    # required parameter rather than an optional one precisely so this cannot
    # drift back: a ``None`` default reads as "not extracted yet" and would
    # re-extract on every stdio call, where ``None`` is also the correct
    # ANSWER — which is how the first attempt at this fix looked correct and
    # changed nothing on the common path.
    from_header = session_id_from_headers(headers)
    return from_header if from_header is not None else fallback


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
    return meta_to_dict(meta)


def _make_emitters(
    *,
    tenant_id: str,
    vendor_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    scrubber: Callable[[Any], Any],
    result_capture_mode: ResultCaptureMode,
) -> tuple[
    _EmitBefore,
    _EmitAfter,
    _EmitError,
    _EmitProactive,
    _EmitSurface,
]:
    """Build five async emitters: ``tool_call_start`` / ``_end`` / ``_error``,
    the synthesised-proactive ``annotation``, and ``surface_snapshot``.

    Each takes the per-call ``session_id`` resolved by
    ``_resolve_call_session_id`` as its first argument — real on stateful
    HTTP, ``fallback_session_id`` otherwise (stdio, or no header found). The
    Console worker also uses ``runtime_meta`` (populated below) for finer
    per-turn correlation per SPEC §11.5, independent of this.
    """

    async def _seq(session_id: str) -> int:
        return await counter.next(session_id)

    async def emit_proactive(
        session_id: str,
        name: str,
        intent: str,
        expected_outcome: str | None,
        workflow: str | None,
        runtime_meta: dict[str, Any] | None,
        client_observed: ClientObserved | None,
        principal: PrincipalWire | None,
        transport_observed: str | None,
    ) -> None:
        # ⚠ Hoisted out of the build thunk: a lambda cannot `await`.
        # A build that then fails burns this number, which is already
        # possible whenever `sink.write` fails, so the gap is not new.
        _seq_n = await _seq(session_id)
        await safe_emit(
            sink,
            lambda: AnnotationEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=_seq_n,
                captured_at=datetime.now(UTC),
                client_observed=client_observed,
                principal=principal,
                transport_observed=transport_observed,
                runtime_meta=runtime_meta,
                payload=AnnotationPayload(
                    intent=intent,
                    expected_outcome=expected_outcome,
                    workflow=workflow,
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
        call_expected: str | None,
        call_workflow: str | None,
        client_observed: ClientObserved | None,
        principal: PrincipalWire | None,
        call_id: str,
        transport_observed: str | None,
    ) -> None:
        injected_any = any(v is not None for v in (call_intent, call_expected, call_workflow))
        # ⚠ Hoisted out of the build thunk: a lambda cannot `await`.
        # A build that then fails burns this number, which is already
        # possible whenever `sink.write` fails, so the gap is not new.
        _seq_n = await _seq(session_id)
        await safe_emit(
            sink,
            lambda: ToolCallStartEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=_seq_n,
                captured_at=datetime.now(UTC),
                client_observed=client_observed,
                principal=principal,
                transport_observed=transport_observed,
                call_id=call_id,
                runtime_meta=runtime_meta,
                payload=ToolCallStartPayload(
                    tool_name=name,
                    params=params,
                    call_intent=call_intent,
                    call_expected=call_expected,
                    call_workflow=call_workflow,
                    intent_source=INTENT_SOURCE_PARAM if injected_any else None,
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
        client_observed: ClientObserved | None,
        principal: PrincipalWire | None,
        call_id: str,
        transport_observed: str | None,
    ) -> None:
        # ⚠ Hoisted out of the build thunk, because the thunk is sync.
        # A build that then fails burns this number, which is already
        # possible whenever `sink.write` fails, so the gap is not new.
        _seq_n = await _seq(session_id)

        # The projection runs INSIDE the thunk: it owns the scrubber call
        # (`_result_capture.py`), so as a statement out here a raising vendor
        # scrubber escaped `safe_emit` entirely and broke the vendor's tool
        # call — `safe_emit` can only guard what the thunk it is handed
        # evaluates. A `def` rather than a lambda because the projection's
        # result is read twice, and a lambda would run it once per read.
        def build() -> ToolCallEndEvent:
            end = end_result_fields(
                mode=result_capture_mode,
                scrubber=scrubber,
                to_jsonable=_result_to_jsonable,
                result=result,
            )
            return ToolCallEndEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=_seq_n,
                captured_at=datetime.now(UTC),
                client_observed=client_observed,
                principal=principal,
                transport_observed=transport_observed,
                call_id=call_id,
                runtime_meta=runtime_meta,
                payload=ToolCallEndPayload(
                    tool_name=name,
                    duration_ms=int(duration_s * 1000),
                    result=end.result,
                    result_capture=end.result_capture,
                ),
            )

        await safe_emit(sink, build, logger)

    async def emit_error(
        session_id: str,
        name: str,
        error_type: str,
        duration_s: float,
        runtime_meta: dict[str, Any] | None,
        client_observed: ClientObserved | None,
        principal: PrincipalWire | None,
        call_id: str,
        transport_observed: str | None,
        raised: BaseException | None,
        result: Any,
    ) -> None:
        """One emitter for BOTH failure shapes (SPEC §11.4.3).

        The caller decides ``error_type`` and which shape this is, because only
        it knows whether it holds a live exception or a returned result whose
        error flag is set. It passes the RAW one of the two; the projection
        onto ``error_body`` / ``result`` happens in the build thunk below, so
        the vendor's scrubber runs inside ``safe_emit``'s guard. ``emit_after``
        takes its ``result`` raw for the same reason.

        Dropping rather than degrading, for both shapes: ``error_body`` is a
        required ``str`` and ``""`` is exactly the withheld/no-message
        ambiguity §11.4.3 exists to warn about, so no value here honestly says
        "we could not scrub this". A dropped event asserts nothing."""
        # ⚠ Hoisted out of the build thunk, because the thunk is sync.
        # A build that then fails burns this number, which is already
        # possible whenever `sink.write` fails, so the gap is not new.
        _seq_n = await _seq(session_id)

        def build() -> ToolCallErrorEvent:
            if raised is not None:
                # No result object exists on a raise, so there is none to
                # record and none to withhold: SPEC §11.4.3(1) leaves this
                # shape unchanged under every mode, because an exception
                # message is the vendor's own code speaking about a call that
                # never returned. Both defaults say exactly that.
                resolved = ErrorResultFields(error_body=str(scrubber(str(raised)))[:2000])
            else:
                # SPEC §11.4.3(2): both result-derived members, decided
                # together, and the projection owns the scrubber call.
                resolved = returned_error_fields(
                    mode=result_capture_mode,
                    scrubber=scrubber,
                    result=result,
                )
            return ToolCallErrorEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=_seq_n,
                captured_at=datetime.now(UTC),
                client_observed=client_observed,
                principal=principal,
                transport_observed=transport_observed,
                call_id=call_id,
                runtime_meta=runtime_meta,
                payload=ToolCallErrorPayload(
                    tool_name=name,
                    error_type=error_type,
                    duration_ms=int(duration_s * 1000),
                    error_body=resolved.error_body,
                    result=resolved.result,
                    result_capture=resolved.result_capture,
                ),
            )

        await safe_emit(sink, build, logger)

    async def emit_surface(session_id: str, digest: str, snapshot: dict[str, Any]) -> None:
        # No ``client_observed``: a surface snapshot describes the SERVER, not
        # whoever happened to trigger the first capture.
        # NOT safe_write — this deliberately lets a write failure propagate so
        # the caller (_wrap_tool_run) can tell success from failure and retry
        # on the next call rather than silently treating the surface as
        # emitted. The caller still fails open overall (SPEC §11.2): it
        # catches this and never lets it reach the vendor's tool call.
        await sink.write(
            SurfaceSnapshotEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=await _seq(session_id),
                captured_at=datetime.now(UTC),
                payload=SurfaceSnapshotPayload(
                    surface_hash=digest,
                    server_info=snapshot["server_info"],
                    capabilities=snapshot["capabilities"],
                    instructions=snapshot["instructions"],
                    tools=snapshot["tools"],
                    seam_augmentations=snapshot["seam_augmentations"],
                ),
            )
        )

    return emit_before, emit_after, emit_error, emit_proactive, emit_surface


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
