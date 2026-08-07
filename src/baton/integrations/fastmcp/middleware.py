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
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from fastmcp.server.dependencies import get_http_headers
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
        resolve_session_id_hook: ResolveSessionIdHook | None = None,
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
        self._resolve_session_id_hook = resolve_session_id_hook
        # tool_name -> {param_name: "injected" | "native"}. Populated at
        # on_list_tools; read at on_call_tool to decide strip-vs-forward, per
        # param, independently. A plain dict (no lock) is safe: all access is
        # on the one asyncio loop, so no statement interleaves.
        self._param_registry: dict[str, dict[str, str]] = {}

    async def on_list_tools(
        self,
        context: MiddlewareContext[ListToolsRequest],
        call_next: CallNext[ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Inject ``user_goal``/``expected_result`` into every wrapped tool's schema.

        Runs on the vendor-true tool list returned by the server; each tool's
        input schema gains optional (``user_goal`` additionally required if
        configured) ``user_goal``/``expected_result`` strings. Both are stripped
        again in ``on_call_tool`` before the vendor handler runs, so the tool
        never sees them. This is the capture path that survives runtimes which
        drop ``instructions`` (Claude Desktop).

        Fail-open: an injection error on one tool leaves that tool untouched
        rather than dropping it from the listing.
        """
        tools = await call_next(context)
        if self._intent_param_mode == "off":
            return tools
        out: list[Tool] = []
        for tool in tools:
            # The annotation tool takes ``intent`` explicitly — don't inject
            # redundant goal params into it.
            if tool.name == self._annotation_tool_name:
                out.append(tool)
                continue
            try:
                new_tool, dispositions = self._inject_goal_params(tool)
            except Exception:
                logger.exception("baton: intent-param injection failed for a tool")
                out.append(tool)
                continue
            if dispositions:
                self._param_registry[tool.name] = dispositions
            out.append(new_tool)
        return out

    def _inject_goal_params(self, tool: Tool) -> tuple[Tool, dict[str, str]]:
        """Return a copy of ``tool`` with ``user_goal``/``expected_result``
        injected, plus each param's disposition (``"injected"``/``"native"``),
        keyed independently — a tool that already declares one of the two names
        is left untouched for that name only, and ``on_call_tool`` forwards the
        vendor's own value for it instead of stripping it. Mirrors
        baton-extmcp's injector."""
        schema = tool.parameters
        if not isinstance(schema, dict):
            return tool, {}
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
            return tool, dispositions
        # Deep-copy so we never mutate the server's canonical registered schema.
        new_schema = copy.deepcopy(schema)
        new_props = new_schema.setdefault("properties", {})
        if not isinstance(new_props, dict):
            return tool, dispositions
        new_props.update(to_inject)
        if (
            dispositions[USER_GOAL_PARAM_NAME] == "injected"
            and self._intent_param_mode == "required"
        ):
            required = new_schema.get("required")
            if isinstance(required, list):
                if USER_GOAL_PARAM_NAME not in required:
                    required.append(USER_GOAL_PARAM_NAME)
            else:
                new_schema["required"] = [USER_GOAL_PARAM_NAME]
        return tool.model_copy(update={"parameters": new_schema}), dispositions

    def _extract_goal_params(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        """Pop the injected ``user_goal``/``expected_result`` from ``arguments``
        in place; return their values independently (either may be absent).

        Mutating in place is what keeps them off the vendor handler — the same
        dict is forwarded downstream."""
        if self._intent_param_mode == "off":
            return None, None
        dispositions = self._param_registry.get(tool_name)
        goal = self._extract_one_goal_param(
            tool_name, arguments, USER_GOAL_PARAM_NAME, dispositions
        )
        expected = self._extract_one_goal_param(
            tool_name, arguments, EXPECTED_RESULT_PARAM_NAME, dispositions
        )
        return goal, expected

    def _extract_one_goal_param(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        param_name: str,
        dispositions: dict[str, str] | None,
    ) -> str | None:
        """Registry dispositions mirror baton-extmcp: ``"native"`` → the param
        is the vendor's, forward untouched; unknown (cold registry — a call
        arrived before we listed) → strip with a warning, safe only because
        the names are reserved. Never raises."""
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

        # Strip the injected goal params IN PLACE, before copying params —
        # ``msg.arguments`` is the same object forwarded to the vendor handler,
        # so the strip keeps ``user_goal``/``expected_result`` off the tool AND
        # out of the captured ``params`` (which must equal the vendor-visible
        # arguments).
        call_intent: str | None = None
        call_expected: str | None = None
        if isinstance(msg.arguments, dict):
            call_intent, call_expected = self._extract_goal_params(tool_name, msg.arguments)
        scrubbed_intent = self._scrubber(call_intent) if call_intent is not None else None
        scrubbed_expected = self._scrubber(call_expected) if call_expected is not None else None

        params = dict(msg.arguments or {})
        raw_meta = self._extract_request_meta(context)
        meta_dict = meta_to_dict(raw_meta)
        runtime = detect_agent_runtime(raw_meta) or self._default_agent_runtime
        # Scrub the meta dict if a scrubber is configured — meta values may
        # carry runtime-supplied identifiers that vendors want filtered.
        scrubbed_meta = self._scrubber(meta_dict) if meta_dict is not None else None

        # Resolved AFTER the goal-param strip + meta extraction above so a
        # configured hook sees vendor-visible ``params`` and unscrubbed
        # ``meta_dict`` — the same shape ``SessionResolutionContext`` carries
        # on the mcp-adapter path.
        session_id = await self._extract_session_id(context, meta_dict, tool_name, params)

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
                        expected_outcome=scrubbed_expected,
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

    async def _extract_session_id(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        meta: dict[str, Any] | None,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """Real per-call session id. Rung 0 (a configured
        ``VendorConfig.resolve_session_id`` hook) is checked first and, on a
        non-empty return, wins outright — see ``docs/design-notes/
        session_resolver_hook.md``. Below that, falls back to FastMCP's own
        ``Context.session_id``, then the process-wide UUID if no session info
        is available.

        Note: unlike the mcp-adapter path, this adapter doesn't implement
        SPEC §3.4 rungs 1-2/4 (``_meta``/header based) below rung 0 — it only
        ever resolves via the standalone ``fastmcp`` library's own session
        concept. See design note D3 for why that gap isn't closed here.
        """
        if self._resolve_session_id_hook is not None:
            headers = self._extract_headers()
            hook_result = await resolve_via_hook(
                self._resolve_session_id_hook,
                SessionResolutionContext(
                    headers=headers, meta=meta, tool_name=tool_name, arguments=arguments
                ),
            )
            if hook_result is not None:
                return hook_result
        return resolve_session_id(context.fastmcp_context, self._fallback_session_id)

    @staticmethod
    def _extract_headers() -> Mapping[str, str] | None:
        """Best-effort HTTP header extraction via FastMCP's context-var-backed
        ``get_http_headers`` (set by ``RequestContextMiddleware`` around the
        whole request, so it's populated by the time ``on_call_tool`` runs).
        Never raises — empty outside a live HTTP request (e.g. stdio).
        ``include_all=True`` so a vendor's hook can read headers the default
        view strips (e.g. ``authorization``, which a session-lookup hook may
        need)."""
        headers = get_http_headers(include_all=True)
        return headers if headers else None

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
