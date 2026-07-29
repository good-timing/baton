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

import copy
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from mcp.types import CallToolRequestParams, ListToolsRequest

from baton._state import ProactiveTracker, SessionCounter, resolve_session_id
from baton._uuid import uuid7
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
        vendor_id: str,
        consent_token: str,
        sink: Sink,
        default_agent_runtime: str = "unknown",
        scrubber: Callable[[Any], Any] = identity_scrub,
        counter: SessionCounter | None = None,
        fallback_session_id: str | None = None,
        annotation_tool_name: str | None = None,
        intent_param_mode: str = "optional",
        proactive_tracker: ProactiveTracker | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._vendor_id = vendor_id
        self._consent_token = consent_token
        self._sink = sink
        self._default_agent_runtime = default_agent_runtime
        self._scrubber = scrubber
        self._counter = counter or SessionCounter()
        self._fallback_session_id = fallback_session_id or f"sdk-{uuid7()}"
        self._annotation_tool_name = annotation_tool_name
        self._intent_param_mode = intent_param_mode
        self._proactive = proactive_tracker or ProactiveTracker()
        # tool_name -> "injected" | "native". Populated at on_list_tools; read at
        # on_call_tool to decide strip-vs-forward. A plain dict (no lock) is safe:
        # all access is on the one asyncio loop, so no statement interleaves.
        self._param_registry: dict[str, str] = {}

    async def on_list_tools(
        self,
        context: MiddlewareContext[ListToolsRequest],
        call_next: CallNext[ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Inject the ``baton_intent`` param into every wrapped tool's schema.

        Runs on the vendor-true tool list returned by the server; each tool's
        input schema gains an optional (or required) ``baton_intent`` string.
        The value is stripped again in ``on_call_tool`` before the vendor
        handler runs, so the tool never sees it. This is the capture path that
        survives runtimes which drop ``instructions`` (Claude Desktop).

        Fail-open: an injection error on one tool leaves that tool untouched
        rather than dropping it from the listing.
        """
        tools = await call_next(context)
        if self._intent_param_mode == "off":
            return tools
        out: list[Tool] = []
        for tool in tools:
            # The annotation tool takes ``intent`` explicitly — don't inject a
            # redundant ``baton_intent`` into it.
            if tool.name == self._annotation_tool_name:
                out.append(tool)
                continue
            try:
                new_tool, disposition = self._inject_intent_param(tool)
            except Exception:
                logger.exception("baton: intent-param injection failed for a tool")
                out.append(tool)
                continue
            if disposition is not None:
                self._param_registry[tool.name] = disposition
            out.append(new_tool)
        return out

    def _inject_intent_param(self, tool: Tool) -> tuple[Tool, str | None]:
        """Return a copy of ``tool`` with ``baton_intent`` injected, plus the
        disposition (``"injected"``/``"native"``/``None`` when unschemable).

        A tool that already declares ``baton_intent`` is left untouched and
        recorded ``"native"`` so ``on_call_tool`` forwards the param to the
        vendor rather than stripping it. Mirrors the proxy's injector."""
        schema = tool.parameters
        if not isinstance(schema, dict):
            return tool, None
        props = schema.get("properties")
        if isinstance(props, dict) and INTENT_PARAM_NAME in props:
            return tool, "native"
        # Deep-copy so we never mutate the server's canonical registered schema.
        new_schema = copy.deepcopy(schema)
        new_props = new_schema.setdefault("properties", {})
        if not isinstance(new_props, dict):
            return tool, None
        new_props[INTENT_PARAM_NAME] = {
            "type": "string",
            "description": build_intent_param_description(),
        }
        if self._intent_param_mode == "required":
            required = new_schema.get("required")
            if isinstance(required, list):
                if INTENT_PARAM_NAME not in required:
                    required.append(INTENT_PARAM_NAME)
            else:
                new_schema["required"] = [INTENT_PARAM_NAME]
        return tool.model_copy(update={"parameters": new_schema}), "injected"

    def _extract_intent_param(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """Pop the injected intent from ``arguments`` in place; return its value.

        Mutating in place is what keeps the value off the vendor handler — the
        same dict is forwarded downstream. Registry dispositions mirror the
        proxy: ``"native"`` → the param is the vendor's, forward untouched;
        unknown (cold registry — a call arrived before we listed) → strip with
        a warning, safe only because the name is namespaced. Never raises."""
        if self._intent_param_mode == "off":
            return None
        if INTENT_PARAM_NAME not in arguments:
            return None
        disposition = self._param_registry.get(tool_name)
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

        session_id = self._extract_session_id(context)

        # Strip the injected intent param IN PLACE, before copying params —
        # ``msg.arguments`` is the same object forwarded to the vendor handler,
        # so the strip keeps ``baton_intent`` off the tool AND out of the
        # captured ``params`` (which must equal the vendor-visible arguments).
        call_intent: str | None = None
        if isinstance(msg.arguments, dict):
            call_intent = self._extract_intent_param(tool_name, msg.arguments)
        scrubbed_intent = self._scrubber(call_intent) if call_intent is not None else None

        params = dict(msg.arguments or {})
        raw_meta = self._extract_request_meta(context)
        meta_dict = meta_to_dict(raw_meta)
        runtime = detect_agent_runtime(raw_meta) or self._default_agent_runtime
        # Scrub the meta dict if a scrubber is configured — meta values may
        # carry runtime-supplied identifiers that vendors want filtered.
        scrubbed_meta = self._scrubber(meta_dict) if meta_dict is not None else None

        # The session's FIRST injected-param intent also becomes a proactive
        # annotation, sequenced BEFORE the tool_call_start it explains (so
        # "proactive before the call it covers" holds downstream). ``claim``
        # dedups per session and is suppressed if a real annotation-tool
        # proactive already fired. Later param intents ride only the start
        # events — a per-call proactive would open one console turn per call.
        if scrubbed_intent is not None and self._proactive.claim(session_id):
            seq_ann = await self._next_seq(session_id)
            await safe_write(
                self._sink,
                AnnotationEvent(
                    tenant_id=self._tenant_id,
                    vendor_id=self._vendor_id,
                    consent_token=self._consent_token,
                    session_id=session_id,
                    sequence_number=seq_ann,
                    captured_at=datetime.now(UTC),
                    agent_runtime=runtime,
                    runtime_meta=scrubbed_meta,
                    payload=AnnotationPayload(
                        intent=scrubbed_intent,
                        intent_source=INTENT_SOURCE_PARAM,
                        tool_name=tool_name,
                    ),
                ),
                logger,
            )

        # tool_call_start — before invoking the vendor handler. safe_write
        # so a sink failure doesn't break the vendor's tool call (SPEC §11.2).
        seq_start = await self._next_seq(session_id)
        await safe_write(
            self._sink,
            ToolCallStartEvent(
                tenant_id=self._tenant_id,
                vendor_id=self._vendor_id,
                consent_token=self._consent_token,
                session_id=session_id,
                sequence_number=seq_start,
                captured_at=datetime.now(UTC),
                agent_runtime=runtime,
                runtime_meta=scrubbed_meta,
                payload=ToolCallStartPayload(
                    tool_name=tool_name,
                    params=self._scrubber(params),
                    call_intent=scrubbed_intent,
                    intent_source=INTENT_SOURCE_PARAM if scrubbed_intent is not None else None,
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
                    vendor_id=self._vendor_id,
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
                vendor_id=self._vendor_id,
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
