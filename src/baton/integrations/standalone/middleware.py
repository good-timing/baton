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
from pydantic_core import to_jsonable_python

from baton._meta_coords import round_meta_coordinates
from baton._state import ProactiveTracker, SessionCounter
from baton._uuid import uuid7
from baton.events import (
    AnnotationEvent,
    AnnotationPayload,
    SurfaceSnapshotEvent,
    SurfaceSnapshotPayload,
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallErrorEvent,
    ToolCallErrorPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from baton.integrations._config import SessionResolutionContext
from baton.integrations._llm_text import (
    EXPECTED_RESULT_PARAM_NAME,
    INTENT_SOURCE_PARAM,
    OVERALL_TASK_PARAM_NAME,
    USER_GOAL_PARAM_NAME,
    build_expected_result_param_description,
    build_overall_task_param_description,
    build_user_goal_param_description,
)
from baton.integrations._surface import assemble_surface, build_seam_augmentations, surface_hash
from baton.integrations.identity_adapter import (
    PRINCIPAL_ID_MODE_HASHED,
    ResolvePrincipalHook,
    resolve_call_principal_id,
)
from baton.integrations.runtime_adapter import (
    UNKNOWN_AGENT_RUNTIME,
    detect_agent_runtime,
    meta_to_dict,
)
from baton.integrations.standalone import _auth
from baton.integrations.standalone._session import (
    extract_headers,
    observe_transport,
    resolve_call_session_id,
)
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
        scrubber: Callable[[Any], Any] = identity_scrub,
        counter: SessionCounter | None = None,
        fallback_session_id: str | None = None,
        annotation_tool_name: str | None = None,
        intent_param_mode: str = "required",
        proactive_tracker: ProactiveTracker | None = None,
        server_meta: dict[str, Any] | None = None,
        principal_id_mode: str = PRINCIPAL_ID_MODE_HASHED,
        principal_id_hmac_key: bytes | None = None,
        resolve_principal_hook: ResolvePrincipalHook | None = None,
        identity_warned: set[str] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._vendor_id = vendor_id
        self._consent_token = consent_token
        self._sink = sink
        self._scrubber = scrubber
        self._counter = counter or SessionCounter()
        self._fallback_session_id = fallback_session_id or f"sdk-{uuid7()}"
        self._annotation_tool_name = annotation_tool_name
        self._intent_param_mode = intent_param_mode
        self._proactive = proactive_tracker or ProactiveTracker()
        self._server_meta = server_meta or {}
        self._principal_id_mode = principal_id_mode
        self._principal_id_hmac_key = principal_id_hmac_key
        self._resolve_principal_hook = resolve_principal_hook
        # Warn-once state for the missing-HMAC-key line. SHARED with the
        # annotation path via install.py so the line is logged once per
        # install, not once per emit path.
        self._identity_warned = identity_warned if identity_warned is not None else set()
        # tool_name -> {param_name: "injected" | "native"}. Populated at
        # on_list_tools; read at on_call_tool to decide strip-vs-forward, per
        # param, independently. A plain dict (no lock) is safe: all access is
        # on the one asyncio loop, so no statement interleaves.
        self._param_registry: dict[str, dict[str, str]] = {}
        # Surface hashes already emitted this process — dedupe per
        # integrations._surface. Plain set (no lock): single asyncio loop,
        # no await between the membership check and the add.
        self._surface_seen: set[str] = set()

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
        await self._maybe_emit_surface_snapshot(tools)
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
        for name, description in (
            (
                USER_GOAL_PARAM_NAME,
                build_user_goal_param_description(intent_param_mode=self._intent_param_mode),
            ),
            (EXPECTED_RESULT_PARAM_NAME, build_expected_result_param_description()),
            (OVERALL_TASK_PARAM_NAME, build_overall_task_param_description()),
        ):
            if name in existing:
                dispositions[name] = "native"
            else:
                dispositions[name] = "injected"
                to_inject[name] = {"type": "string", "description": description}
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

    async def _maybe_emit_surface_snapshot(self, tools: Sequence[Tool]) -> None:
        """Snapshot the vendor-true surface from a ``tools/list`` response and
        emit ``surface_snapshot``, deduped on the surface hash for the process
        lifetime. Fail-open: any error is logged and the response flows on
        untouched — this must never block the vendor's tools/list.

        Runs on the tools ``call_next`` just returned, i.e. BEFORE this
        method's own goal-param injection loop below — so ``tools`` here is
        the vendor's real advertised schema, unmutated (each injected tool is
        a ``model_copy``, never an in-place edit of the originals).
        """
        try:
            # Sorted by name before hashing (matches the mcp-adapter's
            # _SurfaceState.build_snapshot) — call_next()'s list order isn't
            # a meaningful part of the surface's identity, so a pure
            # reordering (e.g. remove+re-add) must not flip surface_hash.
            # ``by_alias=True`` is what keeps this on the WIRE names. mcp 2.0
            # renamed the model fields (``inputSchema`` -> ``input_schema``,
            # ``meta`` -> ``_meta``) and kept the old names as aliases, so a
            # plain dump silently emits ``input_schema`` there — a SPEC §11.4
            # break in the ``surface_snapshot`` payload that depends on which
            # mcp version happened to resolve under fastmcp. The official-SDK
            # adapter never hit it because it hand-builds ``inputSchema``.
            surface_tools = sorted(
                (
                    t.to_mcp_tool().model_dump(mode="json", exclude_none=True, by_alias=True)
                    for t in tools
                    if t.name != self._annotation_tool_name
                ),
                key=lambda t: t["name"],
            )
            surface = assemble_surface(self._server_meta, surface_tools)
            digest = surface_hash(surface)
            if digest in self._surface_seen:
                return

            seam = build_seam_augmentations(
                injected_tool_names=(
                    [self._annotation_tool_name] if self._annotation_tool_name else []
                ),
                intent_param_names=[
                    USER_GOAL_PARAM_NAME,
                    EXPECTED_RESULT_PARAM_NAME,
                    OVERALL_TASK_PARAM_NAME,
                ],
                intent_param_mode=self._intent_param_mode,
            )
            seq = await self._next_seq(self._fallback_session_id)
            event = SurfaceSnapshotEvent(
                tenant_id=self._tenant_id,
                vendor_id=self._vendor_id,
                consent_token=self._consent_token,
                session_id=self._fallback_session_id,
                sequence_number=seq,
                captured_at=datetime.now(UTC),
                agent_runtime=UNKNOWN_AGENT_RUNTIME,
                payload=SurfaceSnapshotPayload(
                    surface_hash=digest,
                    server_info=surface["server_info"],
                    capabilities=surface["capabilities"],
                    instructions=surface["instructions"],
                    tools=surface_tools,
                    seam_augmentations=seam,
                ),
            )
            # Mark seen only AFTER a successful write — the Console's
            # vendor_surfaces upsert is idempotent on surface_hash, so an
            # occasional duplicate send (e.g. two concurrent tools/list
            # calls racing before either marks it) is harmless; marking
            # seen before the write, by contrast, would permanently drop
            # this surface on a single transient sink failure with no retry
            # (unlike every other event type, which gets a fresh attempt
            # next time). ``self._sink.write`` directly, not ``safe_write``
            # — this block needs to observe success/failure, not swallow it.
            try:
                await self._sink.write(event)
            except Exception:
                logger.exception("baton: surface snapshot capture failed")
                return
            self._surface_seen.add(digest)
        except Exception:
            logger.exception("baton: surface snapshot capture failed")

    async def _resolve_dispositions(self, tool_name: str, server: Any) -> dict[str, str] | None:
        """Per-param ``"injected"``/``"native"`` dispositions for ``tool_name``.

        Warm path: whatever ``on_list_tools`` recorded. Cold path: ask the
        server for the tool and compute them on the spot. The cold path is not
        an edge case — fastmcp 4.x's client resolves a ``tools/call`` without
        first issuing ``tools/list``, so on 4.x the registry is empty for
        every tool until something else lists, and the pre-existing
        strip-with-a-warning fallback would eat a vendor's OWN ``user_goal``
        before their handler ever saw it. Reading the server's registry is
        also strictly better evidence than a remembered listing: it is the
        vendor-true schema at call time.

        Never raises — a lookup miss (unknown tool, an unexpected registry
        shape on some future version) returns ``None`` and leaves the caller
        on the original warn-and-strip path, which is safe because these three
        names are reserved.
        """
        cached = self._param_registry.get(tool_name)
        if cached is not None:
            return cached
        if server is None:
            return None
        try:
            tool = await server.get_tool(tool_name)
            _, dispositions = self._inject_goal_params(tool)
        except Exception:
            logger.debug(
                "baton: could not resolve param dispositions for %r from the server registry",
                tool_name,
                exc_info=True,
            )
            return None
        if not dispositions:
            return None
        self._param_registry[tool_name] = dispositions
        return dispositions

    async def _extract_goal_params(
        self, tool_name: str, arguments: dict[str, Any], server: Any
    ) -> tuple[str | None, str | None, str | None]:
        """Pop the injected ``user_goal``/``expected_result``/``overall_task``
        from ``arguments`` in place; return their values independently (any
        may be absent).

        Mutating in place is what keeps them off the vendor handler — the same
        dict is forwarded downstream."""
        if self._intent_param_mode == "off":
            return None, None, None
        dispositions = await self._resolve_dispositions(tool_name, server)
        goal = self._extract_one_goal_param(
            tool_name, arguments, USER_GOAL_PARAM_NAME, dispositions
        )
        expected = self._extract_one_goal_param(
            tool_name, arguments, EXPECTED_RESULT_PARAM_NAME, dispositions
        )
        task = self._extract_one_goal_param(
            tool_name, arguments, OVERALL_TASK_PARAM_NAME, dispositions
        )
        return goal, expected, task

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
        call_task: str | None = None
        if isinstance(msg.arguments, dict):
            fctx = context.fastmcp_context
            call_intent, call_expected, call_task = await self._extract_goal_params(
                tool_name, msg.arguments, getattr(fctx, "fastmcp", None) if fctx else None
            )
        scrubbed_intent = self._scrubber(call_intent) if call_intent is not None else None
        scrubbed_expected = self._scrubber(call_expected) if call_expected is not None else None
        scrubbed_task = self._scrubber(call_task) if call_task is not None else None

        params = dict(msg.arguments or {})
        raw_meta = self._extract_request_meta(context)
        meta_dict = meta_to_dict(raw_meta)
        runtime = (
            detect_agent_runtime(raw_meta, context=context.fastmcp_context, scrubber=self._scrubber)
            or UNKNOWN_AGENT_RUNTIME
        )
        # Identity resolves here, beside the runtime detect: one place per
        # call, producing the FINISHED wire value so the raw principal never
        # reaches the event constructions below. ``None`` on any call where
        # neither provenance resolves, which is most of them.
        #
        # ⚠ This comment used to read "``None`` on stdio and on any
        # unauthenticated call". The stdio half stopped being true when
        # ``resolve_principal`` landed — a hook is the one identity mechanism that
        # works there, and it is the reason the hook exists.
        #
        # The context is built only when a hook exists: ``extract_headers`` is
        # not free on every call of every server that will never set the field.
        identity_hook_context = (
            SessionResolutionContext(
                headers=extract_headers(),
                meta=meta_dict,
                tool_name=tool_name,
                arguments=params,
            )
            if self._resolve_principal_hook is not None
            else None
        )
        call_principal_id = await resolve_call_principal_id(
            _auth.current_access_token(),
            hook=self._resolve_principal_hook,
            hook_context=identity_hook_context,
            mode=self._principal_id_mode,
            tenant_id=self._tenant_id,
            hmac_key=self._principal_id_hmac_key,
            logger=logger,
            warned=self._identity_warned,
        )
        # Round coordinates first, whatever scrubber is configured
        # (``_meta_coords``), then scrub — meta values may carry
        # runtime-supplied identifiers that vendors want filtered.
        scrubbed_meta = (
            self._scrubber(round_meta_coordinates(meta_dict)) if meta_dict is not None else None
        )

        # ⚠ The ordering constraint that used to live here DIED with rung 0
        # (removed 2026-09-12). It read "resolved AFTER the goal-param strip +
        # meta extraction so a configured hook sees vendor-visible ``params``
        # and unscrubbed ``meta_dict``" — true while a hook took those, and
        # meaningless now that this reads headers alone and takes no
        # arguments. Placement here is incidental, not load-bearing: nothing
        # above it feeds it. Said explicitly so the next reader neither
        # preserves a constraint that no longer exists nor thinks they broke
        # one by moving the call.
        # Read once per call, beside the session it conditions. SPEC §3.4
        # rung 5 fires only where this says ``"http"``, because the
        # process-wide fallback below is the CORRECT answer on stdio and a
        # stranger-merging one on a hosted server, and the two are identical
        # in the resolved value.
        #
        # Unconditional, unlike the hook-gated ``extract_headers`` above. The
        # cost argument in that comment is already spent here: the very next
        # line resolves the session, which reads headers on every call anyway.
        call_transport = observe_transport()
        session_id = await self._extract_session_id()

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
                    principal_id=call_principal_id,
                    transport_observed=call_transport,
                    runtime_meta=scrubbed_meta,
                    payload=AnnotationPayload(
                        intent=scrubbed_intent,
                        expected_outcome=scrubbed_expected,
                        workflow=scrubbed_task,
                        intent_source=INTENT_SOURCE_PARAM,
                        tool_name=tool_name,
                    ),
                ),
                logger,
            )

        # The per-call join key (SPEC §11.4). A LOCAL, minted here in the
        # scope that emits all three legs below, which is what makes it
        # per-call by construction and correct across processes — hoisting it
        # onto ``self`` or the module would send one id for a whole session and
        # degrade tier 1 to the FIFO floor it outranks, invisibly (a mispair is
        # a permutation, so every total holds). Never ``ctx.request_id``: it
        # restarts at 1 per connection.
        call_id = str(uuid7())

        # MRTR (mcp>=2.0 / fastmcp 4): a continuation carries the client's answers
        # to an earlier ``InputRequiredResult`` — it is the SAME logical call
        # resuming, not a new one, so it gets no second ``tool_call_start``. The
        # continuation resends the original arguments, so without this every
        # round would re-report the call, its params and its injected intent.
        is_continuation = _is_mrtr_continuation(context)

        # tool_call_start — before invoking the vendor handler. safe_write
        # so a sink failure doesn't break the vendor's tool call (SPEC §11.2).
        if not is_continuation:
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
                    principal_id=call_principal_id,
                    transport_observed=call_transport,
                    call_id=call_id,
                    runtime_meta=scrubbed_meta,
                    payload=ToolCallStartPayload(
                        tool_name=tool_name,
                        params=self._scrubber(params),
                        call_intent=scrubbed_intent,
                        call_expected=scrubbed_expected,
                        call_workflow=scrubbed_task,
                        intent_source=(
                            INTENT_SOURCE_PARAM
                            if any(
                                v is not None
                                for v in (scrubbed_intent, scrubbed_expected, scrubbed_task)
                            )
                            else None
                        ),
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
                    principal_id=call_principal_id,
                    transport_observed=call_transport,
                    call_id=call_id,
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

        # MRTR: an ``InputRequiredResult`` means the call PAUSED to ask the
        # client for input, not that it finished. Emitting an end here would
        # report the ask itself as the call's result — which is what this
        # adapter did before, so one paused-and-resumed call arrived as TWO
        # complete calls, the first carrying the ask as its outcome. The end
        # rides the round that actually completes.
        #
        # ⚠ fastmcp frames this differently on purpose: its own
        # ``InputRequiredToolResult`` docstring calls the ask "the legitimate
        # result of this tool call — not a pause", because each MRTR leg is one
        # complete request/response at the PROTOCOL level. That is true of the
        # wire and not of the vendor's call: SPEC §11.4's legs describe one
        # logical tool call, and the official adapter already treats the two
        # rounds as one. Two adapters disagreeing about what a call IS costs
        # more than either framing gains.
        if _is_mrtr_pause(result):
            return result

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
                principal_id=call_principal_id,
                transport_observed=call_transport,
                call_id=call_id,
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

    async def _extract_session_id(self) -> str:
        """Real per-call session id — SPEC §3.4's ladder, shared with the
        annotation tool so an annotation and the call it describes always
        resolve identically. See ``baton.integrations.standalone._session`` for
        the rungs, including why fastmcp's own ``Context.session_id`` sits
        BELOW the header and is gated to the versions where its cache survives.
        """
        return await resolve_call_session_id(fallback=self._fallback_session_id)

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
        shape for the ``tool_call_end`` payload.

        ``ToolResult`` is a pydantic model on fastmcp 3.x/4.x and a PLAIN
        OBJECT on 2.14.7, our floor — so the ``model_dump`` branch misses
        there and the ``str()`` fallthrough used to put
        ``"<fastmcp.tools.tool.ToolResult object at 0x…>"`` on the wire: a
        memory address in place of the tool's output, changing every run
        (N10, measured 2026-09-09). The duck-typed branch below rebuilds the
        SAME keys ``model_dump`` produces, so one shape reaches the console on
        every supported version. It is keyed on the attributes rather than the
        version because the class has already changed shape once.

        The DEEP conversion is the load-bearing half, and it is about the
        SCRUBBER, not about readability: ``scrub.py`` walks ``dict`` /
        ``list`` / ``str`` and returns anything else untouched, so any model
        left intact anywhere in that tree — a ``mcp.types`` block in
        ``content``, a vendor object nested in ``meta`` — carries its text
        straight past the vendor's scrubber, while the envelope's own pydantic
        dump still puts that text on the wire. Shallow-copying ``meta`` shipped
        `alice@example.com` in the clear on 2.14.7 and redacted on 3.4.2
        (measured; caught in review). ``to_jsonable_python`` is what fastmcp
        2.14.7 itself uses for ``structured_content``, and matches
        ``model_dump(mode="json")`` key for key. ``serialize_unknown`` keeps a
        capture boundary fail-open: an object nothing can serialise degrades to
        its repr — the old behaviour, for that value only — instead of raising.
        That matters because this runs while BUILDING the event, outside
        ``safe_write``'s guard, so a raise here would reach the vendor's tool
        call (cf. ``8b4356d``); ``TypeAdapter(Any).dump_python`` was measured
        and DOES raise, which is what rules it out — and so does ``model_dump``,
        which is why the branch below CATCHES rather than trusting it. That
        catch was missing at first: the fail-open claim held only on the floor
        path, while 3.x/4.x — the shipped one — could raise (caught in review).
        ``pydantic-core`` is no new dependency: pydantic 2.x pins it exactly,
        so it ships wherever pydantic does — the argument ``pyproject.toml``
        already makes for pydantic.

        ⚠ **Fail-open here means "capture is not the cause", not "the call is
        saved".** Measured on 2.14.7 / 3.4.2 / 4.0.2 with a set, bytes, a
        complex number and a bare object in ``meta``: everything the server can
        itself put on the wire this function already handled, and the one value
        that defeats it kills the call upstream with the middleware REMOVED. So
        no shipped case exists where capture breaks a call that would otherwise
        succeed. The guard is for the case that has not happened yet — a result
        the server can send and pydantic cannot dump — which is the shape a
        library change produces.
        """
        if result is None:
            return None
        if hasattr(result, "model_dump"):
            try:
                return result.model_dump(mode="json")
            except Exception:
                return to_jsonable_python(result, serialize_unknown=True, by_alias=False)
        if isinstance(result, (str, int, float, bool, list, dict)):
            return result
        if hasattr(result, "content") or hasattr(result, "structured_content"):
            body: dict[str, Any] = {
                "content": getattr(result, "content", None),
                "structured_content": getattr(result, "structured_content", None),
                "meta": getattr(result, "meta", None),
            }
            # 2.14.7's ToolResult has no ``is_error``; do not fabricate one.
            if hasattr(result, "is_error"):
                body["is_error"] = result.is_error
            # ``by_alias=False`` matches ``model_dump(mode="json")``, which is what
            # 3.x/4.x take above — ``to_jsonable_python`` defaults the other way,
            # and that alone renamed a content block's ``meta`` to ``_meta`` on
            # the floor path. Measured against the other versions, not assumed.
            return to_jsonable_python(body, serialize_unknown=True, by_alias=False)
        return str(result)


def _is_mrtr_continuation(context: MiddlewareContext[Any]) -> bool:
    """True if this middleware invocation is a CONTINUATION of a paused
    multi-round tool call (MRTR, SEP-2322 / mcp>=2.0).

    fastmcp 4 exposes the client's answers to an earlier
    ``InputRequiredResult`` on the request context as ``input_responses``, and
    the server's own resume token as ``request_state``. Either one present
    means a round is resuming rather than starting.

    Read off ``fastmcp_context`` and duck-typed rather than
    ``isinstance``-checked: fastmcp 2.x/3.x have neither property, so this is
    always ``False`` there and the adapter's behaviour on those versions does
    not change at all. ``except Exception`` rather than an enumerated tuple —
    both properties read the live request context, and which error a library
    raises when there is no live request is a GUESS this repo has already got
    wrong once (``8b4356d``: fastmcp raised ``RuntimeError`` where the docstring
    promised ``ValueError``). A capture-path read must never fail the call.
    """
    fctx = context.fastmcp_context
    if fctx is None:
        return False
    try:
        return (
            getattr(fctx, "input_responses", None) is not None
            or getattr(fctx, "request_state", None) is not None
        )
    except Exception:  # pragma: no cover - defensive; see docstring
        return False


def _is_mrtr_pause(result: Any) -> bool:
    """True if ``result`` is fastmcp 4's ``InputRequiredToolResult`` — this
    round asked the client for input instead of completing.

    Duck-typed on the field that distinguishes the subclass rather than
    imported: the class does not exist on fastmcp 2.x/3.x, so an import would
    have to be version-guarded to say the same thing this ``getattr`` says.

    ⚠ NOT the official adapter's discriminator. That one reads
    ``result_type == "input_required"`` off mcp's own ``InputRequiredResult``,
    which is what the mcp seam hands back; the fastmcp seam sees the ask
    already WRAPPED in a ``ToolResult`` subclass whose ``content`` is
    deliberately empty, and the wrapper is what we get. Same event, two seams,
    two shapes.
    """
    return getattr(result, "input_required", None) is not None
