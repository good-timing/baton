"""BatonMiddleware — FastMCP middleware that emits Baton events at the MCP
transport boundary.

Per SPEC §11.2 SDK conformance + CHARTER ADR-4 (thin-emit; never block
vendor's hot path): the middleware wraps every ``on_call_tool`` invocation,
emits ``tool_call_start`` before the vendor handler runs, then either
``tool_call_end`` on success or ``tool_call_error`` on exception.

State managed here is minimal — a per-session sequence-number counter. No
correlation, no detection, no policy (all worker-side per ADR-4). The
middleware is dumb about what events mean; it just emits them faithfully
and lets the Console worker assemble signals downstream.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import CallToolRequestParams
from uuid6 import uuid7

from baton._state import SessionCounter, resolve_session_id
from baton.events import (
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallErrorEvent,
    ToolCallErrorPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from baton.integrations.fastmcp.runtime_adapter import detect_agent_runtime, meta_to_dict
from baton.scrub import identity_scrub
from baton.sinks import Sink, safe_write

logger = logging.getLogger(__name__)


class BatonMiddleware(Middleware):
    """FastMCP middleware that emits Baton events on every tool call."""

    def __init__(
        self,
        *,
        tenant_id: str,
        consent_token: str,
        sink: Sink,
        default_agent_runtime: str = "unknown",
        scrubber: Callable[[Any], Any] = identity_scrub,
        counter: SessionCounter | None = None,
        fallback_session_id: str | None = None,
        annotation_tool_name: str | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._consent_token = consent_token
        self._sink = sink
        self._default_agent_runtime = default_agent_runtime
        self._scrubber = scrubber
        self._counter = counter or SessionCounter()
        self._fallback_session_id = fallback_session_id or f"sdk-{uuid7()}"
        self._annotation_tool_name = annotation_tool_name

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, Any],
    ) -> Any:
        msg = context.message
        tool_name = msg.name

        # Skip tool_call_* emit for the annotation tool — the annotation
        # handler emits its own annotation event with the structured payload.
        if self._annotation_tool_name is not None and tool_name == self._annotation_tool_name:
            return await call_next(context)

        params = dict(msg.arguments or {})
        session_id = self._extract_session_id(context)
        raw_meta = self._extract_request_meta(context)
        meta_dict = meta_to_dict(raw_meta)
        runtime = detect_agent_runtime(raw_meta) or self._default_agent_runtime
        # Scrub the meta dict if a scrubber is configured — meta values may
        # carry runtime-supplied identifiers that vendors want filtered.
        scrubbed_meta = self._scrubber(meta_dict) if meta_dict is not None else None

        # tool_call_start — before invoking the vendor handler. safe_write
        # so a sink failure doesn't break the vendor's tool call (SPEC §11.2).
        seq_start = await self._next_seq(session_id)
        await safe_write(
            self._sink,
            ToolCallStartEvent(
                tenant_id=self._tenant_id,
                consent_token=self._consent_token,
                session_id=session_id,
                sequence_number=seq_start,
                captured_at=datetime.now(UTC),
                agent_runtime=runtime,
                runtime_meta=scrubbed_meta,
                payload=ToolCallStartPayload(
                    tool_name=tool_name,
                    params=self._scrubber(params),
                ),
            ),
            logger,
        )

        called_at = monotonic()
        try:
            result = await call_next(context)
        except BaseException as exc:
            duration_ms = int((monotonic() - called_at) * 1000)
            seq_err = await self._next_seq(session_id)
            await safe_write(
                self._sink,
                ToolCallErrorEvent(
                    tenant_id=self._tenant_id,
                    consent_token=self._consent_token,
                    session_id=session_id,
                    sequence_number=seq_err,
                    captured_at=datetime.now(UTC),
                    agent_runtime=runtime,
                    runtime_meta=scrubbed_meta,
                    payload=ToolCallErrorPayload(
                        tool_name=tool_name,
                        error_type=type(exc).__name__,
                        error_body=str(self._scrubber(str(exc)))[:2000],
                        duration_ms=duration_ms,
                    ),
                ),
                logger,
            )
            raise

        duration_ms = int((monotonic() - called_at) * 1000)
        seq_end = await self._next_seq(session_id)
        await safe_write(
            self._sink,
            ToolCallEndEvent(
                tenant_id=self._tenant_id,
                consent_token=self._consent_token,
                session_id=session_id,
                sequence_number=seq_end,
                captured_at=datetime.now(UTC),
                agent_runtime=runtime,
                runtime_meta=scrubbed_meta,
                payload=ToolCallEndPayload(
                    tool_name=tool_name,
                    result=self._scrubber(self._result_to_jsonable(result)),
                    duration_ms=duration_ms,
                ),
            ),
            logger,
        )
        return result

    # =========================================================================
    # Internal — sequence number + extraction helpers
    # =========================================================================

    async def _next_seq(self, session_id: str) -> int:
        """Atomically increment + return the per-session sequence counter."""
        return await self._counter.next(session_id)

    def _extract_session_id(self, context: MiddlewareContext[CallToolRequestParams]) -> str:
        """Get the session_id from FastMCP's Context, falling back to the
        process-wide UUID if no session info is available."""
        return resolve_session_id(context.fastmcp_context, self._fallback_session_id)

    @staticmethod
    def _extract_request_meta(context: MiddlewareContext[CallToolRequestParams]) -> Any:
        """Pull the wire ``_meta`` from the FastMCP request context.

        FastMCP 3.x strips ``_meta`` from the ``CallToolRequestParams`` it
        hands to middleware (see ``fastmcp.server.server`` — the rebuilt
        message has only ``name`` + ``arguments``). The original meta lives
        on ``fastmcp_context.request_context.meta``.
        """
        fctx = context.fastmcp_context
        if fctx is None:
            return None
        rc = fctx.request_context
        if rc is None:
            return None
        return rc.meta

    @staticmethod
    def _result_to_jsonable(result: Any) -> Any:
        """Convert FastMCP's ToolResult (or anything else) to a JSON-serializable
        shape for the ``tool_call_end`` payload."""
        if result is None:
            return None
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        if isinstance(result, (str, int, float, bool, list, dict)):
            return result
        return str(result)
