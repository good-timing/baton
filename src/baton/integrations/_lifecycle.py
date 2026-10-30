"""The tool list, resource and prompt events (SPEC §11.4.4, §11.4.5), and the
outer ``tools/call`` seam (SPEC §11.4.3), shared by both adapters.

Captured on the low-level server's request handlers, below either library's
own layer, because that is the one place that sees each request a client sent
and the response it got. An adapter supplies the reads that differ between the
two libraries; everything else is decided here, once.

The ``tools/call`` seam strips Baton's params before any vendor code runs,
including a server subclass's own ``call_tool``, and files a call that failed
before the adapter's inner capture ran (``_call_slot``).
"""

from __future__ import annotations

import functools
import logging
import sys
import warnings
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from types import FrameType
from typing import Any

from pydantic import TypeAdapter

from baton._result_capture import ResultCaptureMode
from baton._state import SessionCounter
from baton._uuid import uuid7
from baton.events import Event
from baton.integrations._call_slot import CallSlot, current_slot
from baton.integrations._config import SessionResolutionContext
from baton.integrations._error_result import (
    TOOL_ERROR_TYPE,
    is_error_result,
    raised_error_fields,
    returned_error_fields,
)
from baton.integrations._llm_text import (
    RETIRED_PARAM_NAMES,
    USER_GOAL_PARAM_NAME,
    Goals,
    intent_source,
    pop_goal_params,
    scrub_goals,
)
from baton.integrations._session import ResolvedSession, next_sequence
from baton.integrations.client_observed import clean_runtime_meta, observe_client
from baton.integrations.identity_adapter import (
    ResolvePrincipalHook,
    resolve_caller,
    token_claims,
)
from baton.scrub import scrub_or_none
from baton.sinks import Sink, safe_emit

logger = logging.getLogger(__name__)

_WRAPPED = "_baton_lifecycle"


@dataclass(frozen=True)
class CapturedRequest:
    """What an adapter read off one request, with the reads its tool call path
    uses."""

    meta: dict[str, Any] | None
    headers: Mapping[str, str] | None
    handshake_context: Any
    transport_observed: str | None
    session: ResolvedSession


@dataclass(frozen=True)
class _Family:
    """One request method and the three events it produces."""

    method: str
    mcp1_request_class: str
    event_stem: str
    subject_key: str | None = None
    count_key: str | None = None
    start_params: Callable[[dict[str, Any]], dict[str, Any]] | None = None


@dataclass(frozen=True)
class CallSeam:
    annotation_tool_name: str | None
    result_capture_mode: ResultCaptureMode
    dispositions: Callable[[str], Awaitable[dict[str, str] | None]]
    """The tool's param dispositions, or ``None`` when the adapter has none,
    which strips ``user_goal`` alone (``pop_goal_params``)."""
    knows_tool: Callable[[str], bool | None] = lambda _name: None
    """Whether the server has the tool; ``None`` when the adapter cannot say."""


# SPEC §11.4.4 records that the two start payloads build ``params``
# differently: a read sends every param, a prompt get sends ``arguments`` alone.
_FAMILIES = (
    _Family("tools/list", "ListToolsRequest", "tool_list", count_key="tools"),
    _Family("resources/list", "ListResourcesRequest", "resource_list", count_key="resources"),
    _Family(
        "resources/read",
        "ReadResourceRequest",
        "resource_read",
        subject_key="uri",
        start_params=lambda params: params,
    ),
    _Family("prompts/list", "ListPromptsRequest", "prompt_list", count_key="prompts"),
    _Family(
        "prompts/get",
        "GetPromptRequest",
        "prompt_get",
        subject_key="name",
        start_params=lambda params: params.get("arguments") or {},
    ),
)

_EVENT: TypeAdapter[Event] = TypeAdapter(Event)


def install_lifecycle_capture(
    find_server: Callable[[], Any],
    *,
    tenant_id: str,
    vendor_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    scrubber: Callable[[Any], Any],
    resolve_principal_hook: ResolvePrincipalHook | None,
    read_request: Callable[[Any], Awaitable[CapturedRequest]],
    read_access_token: Callable[[], Any],
    call_seam: CallSeam | None = None,
) -> None:
    """Send the tool list, resource and prompt events of the low-level ``mcp``
    server ``find_server`` returns, and with ``call_seam``, install the outer
    ``tools/call`` seam.

    ``read_request`` is given the low-level request context, or ``None`` where
    there is none. Never raises: a version with no known handler layout is
    logged and served without these events. A second install on one server
    changes nothing.
    """

    async def opened(
        family: _Family,
        read_request_context: Callable[[], Any],
        params: Any,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], ResolvedSession] | None:
        """The envelope, the request's params, the subject and the session, or
        ``None`` when a read failed and the request is served without events."""
        try:
            request = await read_request(read_request_context())
            caller = await resolve_caller(
                hook=resolve_principal_hook,
                hook_context=(
                    SessionResolutionContext(
                        headers=request.headers,
                        meta=request.meta,
                        tool_name=tool_name,
                        arguments=arguments or {},
                        claims=token_claims(read_access_token()),
                    )
                    if resolve_principal_hook is not None
                    else None
                ),
                logger=logger,
            )
            sent = _params_sent(params)
            subject = (
                {}
                if family.subject_key is None
                else {family.subject_key: str(sent.get(family.subject_key) or "")}
            )
            envelope = {
                "tenant_id": tenant_id,
                "vendor_id": vendor_id,
                "consent_token": consent_token,
                "session_id": request.session.id,
                "session_id_origin": request.session.origin,
                "client_observed": observe_client(
                    request.meta,
                    context=request.handshake_context,
                    headers=request.headers,
                    scrubber=scrubber,
                ),
                "principal": caller.principal,
                "organization": caller.organization,
                "transport_observed": request.transport_observed,
                "runtime_meta": clean_runtime_meta(scrubber, request.meta, logger),
            }
            return envelope, sent, subject, request.session
        except Exception:
            logger.exception(
                "baton: %s capture failed; the request is served without it", family.method
            )
            return None

    async def emit(
        envelope: dict[str, Any],
        session: ResolvedSession,
        event_type: str,
        build_payload: Callable[[], dict[str, Any]],
    ) -> None:
        sequence_number = await next_sequence(counter, session)
        await safe_emit(
            sink,
            lambda: _EVENT.validate_python(
                {
                    **envelope,
                    "event_type": event_type,
                    "sequence_number": sequence_number,
                    "captured_at": datetime.now(UTC),
                    "payload": build_payload(),
                }
            ),
            logger,
        )

    async def around(
        family: _Family,
        read_request_context: Callable[[], Any],
        params: Any,
        serve: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        request = await opened(family, read_request_context, params)
        if request is None:
            return await serve(params)
        envelope, sent, subject, session = request

        async def emit_leg(leg: str, build_payload: Callable[[], dict[str, Any]]) -> None:
            await emit(
                envelope,
                session,
                f"{family.event_stem}_{leg}",
                lambda: _scrubbed(scrubber, {**subject, **build_payload()}),
            )

        await emit_leg(
            "start",
            lambda: {} if family.start_params is None else {"params": family.start_params(sent)},
        )
        started_at = monotonic()
        try:
            result = await serve(params)
        except BaseException as exc:
            raised = exc
            duration_ms = int((monotonic() - started_at) * 1000)
            await emit_leg(
                "error",
                lambda: {
                    "error_type": type(raised).__name__,
                    "error_body": str(raised),
                    "duration_ms": duration_ms,
                },
            )
            raise
        duration_ms = int((monotonic() - started_at) * 1000)
        await emit_leg(
            "end",
            lambda: (
                {"duration_ms": duration_ms}
                if family.count_key is None
                else {"count": _count(result, family.count_key), "duration_ms": duration_ms}
            ),
        )
        return result

    async def around_call(
        call_seam: CallSeam,
        _family: _Family,
        read_request_context: Callable[[], Any],
        params: Any,
        serve: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        name = getattr(params, "name", None)
        arguments = getattr(params, "arguments", None)
        if not isinstance(name, str) or name == call_seam.annotation_tool_name:
            return await serve(params)
        arguments = arguments if isinstance(arguments, dict) else None
        goals: Goals | None = None
        try:
            dispositions = await call_seam.dispositions(name)
            # No dispositions and a tool the server may still have: the inner
            # capture resolves them in its own context, so the strip waits.
            strip_here = dispositions is not None or call_seam.knows_tool(name) is False
            if strip_here and arguments is not None:
                if _BATON_PARAMS.isdisjoint(arguments):
                    goals = (None, None, None)
                else:
                    arguments = dict(arguments)
                    goals = pop_goal_params(name, arguments, dispositions)
                    params = params.model_copy(update={"arguments": arguments})
        except Exception:
            logger.exception("baton: the tools/call strip failed; the inner capture strips instead")
            goals = None
        slot = CallSlot(goals=goals)
        token = current_slot.set(slot)
        started_at = monotonic()
        try:
            try:
                result = await serve(params)
            except Exception as exc:
                if not slot.claimed:
                    await refused(
                        call_seam,
                        read_request_context,
                        params,
                        name,
                        arguments,
                        goals,
                        started_at,
                        raised=exc,
                    )
                raise
            root = getattr(result, "root", result)
            if not slot.claimed and is_error_result(root):
                await refused(
                    call_seam,
                    read_request_context,
                    params,
                    name,
                    arguments,
                    goals,
                    started_at,
                    result=root,
                )
            return result
        finally:
            current_slot.reset(token)

    async def refused(
        call_seam: CallSeam,
        read_request_context: Callable[[], Any],
        params: Any,
        name: str,
        arguments: dict[str, Any] | None,
        goals: Goals | None,
        started_at: float,
        *,
        raised: Exception | None = None,
        result: Any = None,
    ) -> None:
        """Send the pair for a call that failed before the inner capture ran."""
        duration_ms = int((monotonic() - started_at) * 1000)
        try:
            if goals is None and arguments is not None:
                # The outer seam left the strip to an inner capture that never
                # ran, so the reserved name comes out of the reported copy here.
                arguments = dict(arguments)
                goals = pop_goal_params(name, arguments, None)
            request = await opened(_CALL_FAMILY, read_request_context, params, name, arguments)
            if request is None:
                return
            envelope, _, _, session = request
            envelope = {**envelope, "call_id": str(uuid7())}
            scrubbed = scrub_goals(scrubber, goals or (None, None, None), logger)
            # SPEC §11.4.3: an unknown tool's message is the library's and stays
            # under "off"; a refusal's is the vendor's and is withheld.
            if call_seam.knows_tool(name) is False:
                failure_kind, mode = "unknown_tool", "full"
            else:
                failure_kind, mode = "refused", call_seam.result_capture_mode
            if raised is not None:
                error_type = type(raised).__name__
                fields = raised_error_fields(
                    mode=mode, scrubber=scrubber, raised=raised, tool_error=None
                )
            else:
                error_type = TOOL_ERROR_TYPE
                fields = returned_error_fields(mode=mode, scrubber=scrubber, result=result)
            start_payload = {
                "tool_name": name,
                "params": scrub_or_none(scrubber, arguments or {}, "params", logger) or {},
                "call_intent": scrubbed[0],
                "call_expected": scrubbed[1],
                "call_workflow": scrubbed[2],
                "intent_source": intent_source(scrubbed),
            }
            error_payload = {
                "tool_name": name,
                "error_type": error_type,
                "error_body": fields.error_body,
                "duration_ms": duration_ms,
                "result": fields.result,
                "result_capture": fields.result_capture,
                "failure_kind": failure_kind,
            }
            await emit(envelope, session, "tool_call_start", lambda: start_payload)
            await emit(envelope, session, "tool_call_error", lambda: error_payload)
        except Exception:
            logger.exception("baton: a refused tool call was not captured")

    try:
        server = find_server()
        for family in _FAMILIES:
            _wrap_handler(server, family, around)
        if call_seam is not None:
            _wrap_handler(server, _CALL_FAMILY, functools.partial(around_call, call_seam))
    except Exception:
        logger.exception(
            "baton: tool list, resource and prompt events may be missing; "
            "this mcp version has no known request handler layout"
        )


def warn_if_results_are_withheld(result_capture_mode: ResultCaptureMode) -> None:
    """SPEC §11.4.4: a failed resource or prompt request sends its message
    under every capture mode, and a vendor who withholds results is told."""
    if result_capture_mode == "off":
        warnings.warn(
            "result_capture_mode='off' does not withhold the message of a failed "
            "resource or prompt request. resource_list_error, resource_read_error, "
            "prompt_list_error and prompt_get_error carry that message, which can "
            "include text your handler raised. Raise a message that names the "
            "resource, not its contents.",
            UserWarning,
            stacklevel=_frames_to_the_vendor(),
        )


def _frames_to_the_vendor() -> int:
    """The ``stacklevel`` that names the line that called into this package."""
    level = 2
    frame: FrameType | None = sys._getframe(2)
    while frame is not None and frame.f_globals.get("__name__", "").split(".")[0] == "baton":
        level, frame = level + 1, frame.f_back
    return level


def _scrubbed(scrubber: Callable[[Any], Any], payload: dict[str, Any]) -> dict[str, Any]:
    # ``uri`` and ``name`` are the caller's text, and a read's ``params`` holds
    # the ``uri`` too, so they go to the scrubber as one dict, and only what it
    # returns is sent:
    # ``test_the_scrubber_sees_the_subject_and_the_params_together``.
    callers = {key: payload[key] for key in ("uri", "name", "params") if key in payload}
    scrubbed = {key: value for key, value in payload.items() if key not in callers}
    if callers:
        scrubbed.update(scrubber(callers))
    if "error_body" in scrubbed:
        scrubbed["error_body"] = str(scrubber(scrubbed["error_body"]))[:2000]
    return scrubbed


def _params_sent(params: Any) -> dict[str, Any]:
    """The request's params as the client sent them, without ``_meta``, which
    is on the envelope."""
    if isinstance(params, Mapping):
        sent = dict(params)
    elif hasattr(params, "model_dump"):
        sent = params.model_dump(by_alias=True, exclude_none=True, mode="json")
    else:
        return {}
    sent.pop("_meta", None)
    return sent


def _count(result: Any, key: str) -> int:
    """How many items the response held, or 0 for a result this cannot read."""
    # mcp <2 wraps the handler's result in a ``ServerResult``; mcp 2.x does not.
    root = getattr(result, "root", result)
    items = root.get(key) if isinstance(root, Mapping) else getattr(root, key, None)
    return len(items) if isinstance(items, list | tuple) else 0


def _wrap_handler(
    server: Any,
    family: _Family,
    around: Callable[
        [_Family, Callable[[], Any], Any, Callable[[Any], Awaitable[Any]]], Awaitable[Any]
    ],
) -> None:
    """Route every ``family.method`` request a client sends through ``around``,
    which may serve it with replaced params. A server with no handler for the
    method is left as it is.

    Both libraries bind their handlers when the server is constructed, so the
    entry the low-level server dispatches on is what gets swapped.
    """
    handlers = getattr(server, "request_handlers", None)
    if handlers is None:  # mcp 2.x
        entry = server.get_request_handler(family.method)
        if entry is None or getattr(entry.handler, _WRAPPED, False):
            return
        serve_params = entry.handler

        async def handle_params(request_context: Any, params: Any) -> Any:
            return await around(
                family,
                lambda: request_context,
                params,
                lambda served: serve_params(request_context, served),
            )

        setattr(handle_params, _WRAPPED, True)
        server.add_request_handler(family.method, entry.params_type, handle_params)
        return

    import mcp.types as mcp_types

    request_type = getattr(mcp_types, family.mcp1_request_class)
    serve_request = handlers.get(request_type)
    if serve_request is None or getattr(serve_request, _WRAPPED, False):
        return

    async def handle_request(request: Any) -> Any:
        # mcp 1.x calls its list handler itself, with no request, to fill its
        # tool cache while it serves a call. No client asked:
        # ``test_only_a_request_from_the_client_is_a_listing``.
        if request is None:
            return await serve_request(request)
        params = getattr(request, "params", None)
        return await around(
            family,
            lambda: server.request_context,
            params,
            lambda served: serve_request(
                request if served is params else request.model_copy(update={"params": served})
            ),
        )

    setattr(handle_request, _WRAPPED, True)
    handlers[request_type] = handle_request


_CALL_FAMILY = _Family("tools/call", "CallToolRequest", "tool_call")
_BATON_PARAMS = frozenset((USER_GOAL_PARAM_NAME, *RETIRED_PARAM_NAMES))
