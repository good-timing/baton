"""The tool list events (SPEC §11.4.5), shared by both adapters.

Captured on the low-level server's ``tools/list`` handler, below either
library's own layer, because that is the one place that sees each request a
client sent and the response it got. An adapter supplies the reads that differ
between the two libraries; everything else is decided here, once.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from baton._meta_coords import round_meta_coordinates
from baton._state import SessionCounter
from baton.events import (
    Event,
    ToolListEndEvent,
    ToolListEndPayload,
    ToolListErrorEvent,
    ToolListErrorPayload,
    ToolListStartEvent,
    ToolListStartPayload,
)
from baton.integrations._config import SessionResolutionContext
from baton.integrations.client_observed import observe_client
from baton.integrations.identity_adapter import (
    ResolvePrincipalHook,
    resolve_call_principal,
    token_claims,
)
from baton.scrub import scrub_or_none
from baton.sinks import Sink, safe_emit

logger = logging.getLogger(__name__)

_WRAPPED = "_baton_tool_list"


@dataclass(frozen=True)
class ListingRequest:
    """What an adapter read off one ``tools/list`` request, with the reads its
    tool call path uses."""

    meta: dict[str, Any] | None
    headers: Mapping[str, str] | None
    handshake_context: Any
    transport_observed: str | None
    session_id: str


def install_tool_list_capture(
    find_server: Callable[[], Any],
    *,
    tenant_id: str,
    vendor_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    scrubber: Callable[[Any], Any],
    resolve_principal_hook: ResolvePrincipalHook | None,
    read_request: Callable[[Any], Awaitable[ListingRequest]],
    read_access_token: Callable[[], Any],
) -> None:
    """Send the tool list events of the low-level ``mcp`` server ``find_server``
    returns.

    ``read_request`` is given the low-level request context, or ``None`` where
    there is none. Never raises: a version with no known list handler is
    logged and served without these events. A second install on one server
    changes nothing.
    """

    async def envelope_of(read_request_context: Callable[[], Any]) -> dict[str, Any] | None:
        try:
            request = await read_request(read_request_context())
            principal = await resolve_call_principal(
                hook=resolve_principal_hook,
                hook_context=(
                    SessionResolutionContext(
                        headers=request.headers,
                        meta=request.meta,
                        tool_name=None,
                        arguments={},
                        claims=token_claims(read_access_token()),
                    )
                    if resolve_principal_hook is not None
                    else None
                ),
                logger=logger,
            )
            return {
                "tenant_id": tenant_id,
                "vendor_id": vendor_id,
                "consent_token": consent_token,
                "session_id": request.session_id,
                "client_observed": observe_client(
                    request.meta,
                    context=request.handshake_context,
                    headers=request.headers,
                    scrubber=scrubber,
                ),
                "principal": principal,
                "transport_observed": request.transport_observed,
                "runtime_meta": scrub_or_none(
                    scrubber,
                    round_meta_coordinates(request.meta) if request.meta is not None else None,
                    "_meta",
                    logger,
                ),
            }
        except Exception:
            logger.exception("baton: tool list capture failed; the listing is served without it")
            return None

    async def around(
        read_request_context: Callable[[], Any], serve: Callable[[], Awaitable[Any]]
    ) -> Any:
        envelope = await envelope_of(read_request_context)
        if envelope is None:
            return await serve()

        async def emit(build: Callable[[dict[str, Any]], Event]) -> None:
            sequence_number = await counter.next(envelope["session_id"])
            await safe_emit(
                sink,
                lambda: build(
                    {
                        **envelope,
                        "sequence_number": sequence_number,
                        "captured_at": datetime.now(UTC),
                    }
                ),
                logger,
            )

        await emit(lambda common: ToolListStartEvent(**common, payload=ToolListStartPayload()))
        started_at = monotonic()
        try:
            result = await serve()
        except BaseException as exc:
            raised = exc
            duration_ms = int((monotonic() - started_at) * 1000)
            await emit(
                lambda common: ToolListErrorEvent(
                    **common,
                    payload=ToolListErrorPayload(
                        error_type=type(raised).__name__,
                        error_body=str(scrubber(str(raised)))[:2000],
                        duration_ms=duration_ms,
                    ),
                )
            )
            raise
        duration_ms = int((monotonic() - started_at) * 1000)
        await emit(
            lambda common: ToolListEndEvent(
                **common,
                payload=ToolListEndPayload(count=_tool_count(result), duration_ms=duration_ms),
            )
        )
        return result

    try:
        _wrap_tool_list_handler(find_server(), around)
    except Exception:
        logger.exception("baton: no tool list events; this mcp version has no known list handler")


def _tool_count(result: Any) -> int:
    # mcp <2 wraps the handler's result in a ``ServerResult``; mcp 2.x does not.
    return len(getattr(result, "root", result).tools)


def _wrap_tool_list_handler(
    server: Any,
    around: Callable[[Callable[[], Any], Callable[[], Awaitable[Any]]], Awaitable[Any]],
) -> None:
    """Route every ``tools/list`` request a client sends through
    ``around(read_request_context, serve)``.

    Both libraries bind their list handler when the server is constructed, so
    the entry the low-level server dispatches on is what gets swapped.
    """
    handlers = getattr(server, "request_handlers", None)
    if handlers is None:  # mcp 2.x
        entry = server.get_request_handler("tools/list")
        serve_params = entry.handler
        if getattr(serve_params, _WRAPPED, False):
            return

        async def handle_params(request_context: Any, params: Any) -> Any:
            return await around(
                lambda: request_context, lambda: serve_params(request_context, params)
            )

        setattr(handle_params, _WRAPPED, True)
        server.add_request_handler("tools/list", entry.params_type, handle_params)
        return

    import mcp.types as mcp_types

    serve_request = handlers[mcp_types.ListToolsRequest]
    if getattr(serve_request, _WRAPPED, False):
        return

    async def handle_request(request: Any) -> Any:
        # mcp 1.x calls this handler itself, with no request, to fill its tool
        # cache while it serves a call. No client asked:
        # ``test_only_a_request_from_the_client_is_a_listing``.
        if request is None:
            return await serve_request(request)
        return await around(lambda: server.request_context, lambda: serve_request(request))

    setattr(handle_request, _WRAPPED, True)
    handlers[mcp_types.ListToolsRequest] = handle_request
