"""BatonMiddleware — FastMCP middleware that emits Baton events at the MCP
transport boundary.

Per SPEC §11.2 SDK conformance + CHARTER ADR-4 (thin-emit; never block
vendor's hot path): the middleware wraps every ``on_call_tool`` invocation,
emits ``tool_call_start`` before the vendor handler runs, then either
``tool_call_end`` on success or ``tool_call_error`` on FAILURE — which MCP
expresses two ways (SPEC §11.4.3): the handler raises, or it returns a result
carrying MCP's error flag on a 200.

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

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool
from mcp.types import CallToolRequestParams, ListToolsRequest
from pydantic_core import to_jsonable_python

from baton._result_capture import ResultCaptureMode, end_result_fields
from baton._state import SessionCounter
from baton._uuid import uuid7
from baton.events import (
    SurfaceSnapshotEvent,
    SurfaceSnapshotPayload,
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallErrorEvent,
    ToolCallErrorPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)
from baton.integrations._annotation_payload import with_tool_name_required
from baton.integrations._call_slot import claim_call
from baton.integrations._config import SessionResolutionContext
from baton.integrations._error_result import (
    TOOL_ERROR_TYPE,
    is_error_result,
    raised_error_fields,
    returned_error_fields,
    vendor_exception,
)
from baton.integrations._llm_text import (
    USER_GOAL_PARAM_NAME,
    drop_retired_params,
    inject_goal_params,
    instructions_suffix_served,
    intent_source,
    pop_goal_params,
    required_param_names,
    scrub_goals,
)
from baton.integrations._session import next_sequence
from baton.integrations._surface import (
    SentSurfaces,
    assemble_surface,
    build_seam_augmentations,
    surface_hash,
)
from baton.integrations.client_observed import clean_runtime_meta, meta_to_dict, observe_client
from baton.integrations.identity_adapter import (
    ResolvePrincipalHook,
    resolve_caller,
    token_claims,
)
from baton.integrations.standalone import _auth
from baton.integrations.standalone._session import (
    extract_headers,
    observe_transport,
    resolve_call_session,
)
from baton.scrub import identity_scrub
from baton.sinks import Sink, safe_emit

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
        result_capture_mode: ResultCaptureMode = "full",
        server_meta: dict[str, Any] | None = None,
        resolve_principal_hook: ResolvePrincipalHook | None = None,
        on_native_user_goal: Callable[[str], None] | None = None,
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
        self._result_capture_mode = result_capture_mode
        self._server_meta = server_meta or {}
        self._on_native_user_goal = on_native_user_goal
        self._resolve_principal_hook = resolve_principal_hook
        # tool_name -> {param_name: "injected" | "native"}. Populated at
        # on_list_tools; read at on_call_tool to decide strip-vs-forward, per
        # param, independently. A plain dict (no lock) is safe: all access is
        # on the one asyncio loop, so no statement interleaves.
        self._param_registry: dict[str, dict[str, str]] = {}
        # No lock: two concurrent listings of a new surface may both send it,
        # and the consumer stores a hash once.
        self._sent_surfaces = SentSurfaces()

    async def on_list_tools(
        self,
        context: MiddlewareContext[ListToolsRequest],
        call_next: CallNext[ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Inject ``user_goal`` into every wrapped tool's schema.

        Runs on the vendor-true tool list returned by the server; each tool's
        input schema gains the ``user_goal`` string, listed as required under
        ``required`` mode. It is stripped again in ``on_call_tool`` before the
        vendor handler runs, so the tool never sees it. This is the capture path
        that survives runtimes which drop ``instructions`` (Claude Desktop).

        Fail-open: an injection error on one tool leaves that tool untouched
        rather than dropping it from the listing.
        """
        tools = await call_next(context)
        await self._maybe_emit_surface_snapshot(tools)
        out: list[Tool] = []
        for tool in tools:
            # The annotation tool takes ``user_goal`` itself — don't inject
            # redundant goal params into it.
            if tool.name == self._annotation_tool_name:
                out.append(self._annotation_tool_as_advertised(tool))
                continue
            try:
                new_tool, dispositions = self._inject_goal_params(tool)
            except Exception:
                logger.exception("baton: intent-param injection failed for a tool")
                out.append(tool)
                continue
            if dispositions:
                self._param_registry[tool.name] = dispositions
            if (
                dispositions.get(USER_GOAL_PARAM_NAME) == "native"
                and self._on_native_user_goal is not None
            ):
                self._on_native_user_goal(tool.name)
            out.append(new_tool)
        return out

    def _annotation_tool_as_advertised(self, tool: Tool) -> Tool:
        try:
            return tool.model_copy(update={"parameters": with_tool_name_required(tool.parameters)})
        except Exception:
            logger.exception("baton: could not list tool_name as required")
            return tool

    def _inject_goal_params(self, tool: Tool) -> tuple[Tool, dict[str, str]]:
        """Return a copy of ``tool`` with ``user_goal`` injected
        (``inject_goal_params``), plus each name's disposition."""
        # A copy, so the server's registered schema is never changed.
        schema = copy.deepcopy(tool.parameters)
        dispositions = inject_goal_params(schema, intent_param_mode=self._intent_param_mode)
        if dispositions.get(USER_GOAL_PARAM_NAME) != "injected":
            return tool, dispositions
        return tool.model_copy(update={"parameters": schema}), dispositions

    async def _maybe_emit_surface_snapshot(self, tools: Sequence[Tool]) -> None:
        """Snapshot the vendor-true surface from a ``tools/list`` response and
        emit ``surface_snapshot`` unless its hash is among the last ones sent.
        Fail-open: any error is logged and the response flows on
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
            if self._sent_surfaces.was_sent(digest):
                return

            seam = build_seam_augmentations(
                injected_tool_names=(
                    [self._annotation_tool_name] if self._annotation_tool_name else []
                ),
                intent_param_names=[USER_GOAL_PARAM_NAME],
                intent_param_mode=self._intent_param_mode,
                required_names=list(
                    required_param_names(intent_param_mode=self._intent_param_mode)
                ),
                instructions_suffix=instructions_suffix_served(
                    annotation_tool_name=self._annotation_tool_name, surface_tools=surface_tools
                ),
            )
            seq = await self._counter.next(self._fallback_session_id)
            event = SurfaceSnapshotEvent(
                tenant_id=self._tenant_id,
                vendor_id=self._vendor_id,
                consent_token=self._consent_token,
                session_id=self._fallback_session_id,
                session_id_origin="minted",
                sequence_number=seq,
                captured_at=datetime.now(UTC),
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
            self._sent_surfaces.mark(digest)
        except Exception:
            logger.exception("baton: surface snapshot capture failed")

    async def resolve_dispositions(self, tool_name: str, server: Any) -> dict[str, str] | None:
        """Per-name dispositions for ``tool_name`` (see ``_inject_goal_params``).

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
        on the original warn-and-strip path, which is safe because ``user_goal`` is
        reserved; the retired names are left alone (``pop_goal_params``).
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
        """``pop_goal_params`` for this call. Mutating ``arguments`` in place
        is what keeps them off the vendor handler: the same dict is forwarded
        downstream."""
        dispositions = await self.resolve_dispositions(tool_name, server)
        return pop_goal_params(tool_name, arguments, dispositions)

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
            # fastmcp rejects arguments a tool does not declare, so a stale
            # client's report would fail; mcp ignores them.
            if isinstance(msg.arguments, dict):
                drop_retired_params(msg.arguments)
            return await call_next(context)

        # Strip the injected goal params IN PLACE, before copying params —
        # ``msg.arguments`` is the same object forwarded to the vendor handler,
        # so the strip keeps ``user_goal`` off the tool AND
        # out of the captured ``params`` (which must equal the vendor-visible
        # arguments).
        call_intent: str | None = None
        call_expected: str | None = None
        call_task: str | None = None
        claimed = claim_call()
        if claimed is not None:
            call_intent, call_expected, call_task = claimed
        elif isinstance(msg.arguments, dict):
            fctx = context.fastmcp_context
            call_intent, call_expected, call_task = await self._extract_goal_params(
                tool_name, msg.arguments, getattr(fctx, "fastmcp", None) if fctx else None
            )
        # scrub_or_none, not a bare call: these run OUTSIDE any `safe_emit`
        # build thunk, so a vendor scrubber that raises here would break the
        # vendor's tool call (SPEC §11.2). An unscrubbed goal param degrades to
        # no captured intent, which is what `None` already means downstream.
        scrubbed_intent, scrubbed_expected, scrubbed_task = scrub_goals(
            self._scrubber, (call_intent, call_expected, call_task), logger
        )

        params = dict(msg.arguments or {})
        meta_dict = meta_to_dict(self._extract_request_meta(context))
        call_headers = extract_headers()
        client = observe_client(
            meta_dict,
            context=context.fastmcp_context,
            headers=call_headers,
            scrubber=self._scrubber,
        )
        # Identity resolves here, in one place per call, producing the
        # FINISHED wire values so the raw principal never reaches the event
        # constructions below. Empty on any call where
        # no hook is set or it has no answer, which is most of them.
        #
        # The context is built only when a hook exists: the token read is not
        # free on every call of every server that will never set the field.
        identity_hook_context = (
            SessionResolutionContext(
                headers=call_headers,
                meta=meta_dict,
                tool_name=tool_name,
                arguments=params,
                claims=token_claims(_auth.current_access_token()),
            )
            if self._resolve_principal_hook is not None
            else None
        )
        caller = await resolve_caller(
            hook=self._resolve_principal_hook,
            hook_context=identity_hook_context,
            logger=logger,
        )
        scrubbed_meta = clean_runtime_meta(self._scrubber, meta_dict, logger)

        # Read once per call, beside the session it conditions: SPEC §3.4
        # rung 5 fires only where this says ``"http"``.
        call_transport = observe_transport()
        call_session = await resolve_call_session(
            headers=call_headers,
            fallback=self._fallback_session_id,
            transport_observed=call_transport,
            context=context.fastmcp_context,
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
            seq_start = await next_sequence(self._counter, call_session)
            await safe_emit(
                self._sink,
                lambda: ToolCallStartEvent(
                    tenant_id=self._tenant_id,
                    vendor_id=self._vendor_id,
                    consent_token=self._consent_token,
                    session_id=call_session.id,
                    sequence_number=seq_start,
                    captured_at=datetime.now(UTC),
                    client_observed=client,
                    principal=caller.principal,
                    organization=caller.organization,
                    transport_observed=call_transport,
                    session_id_origin=call_session.origin,
                    call_id=call_id,
                    runtime_meta=scrubbed_meta,
                    payload=ToolCallStartPayload(
                        tool_name=tool_name,
                        params=self._scrubber(params),
                        call_intent=scrubbed_intent,
                        call_expected=scrubbed_expected,
                        call_workflow=scrubbed_task,
                        intent_source=intent_source(
                            (scrubbed_intent, scrubbed_expected, scrubbed_task)
                        ),
                    ),
                ),
                logger,
            )

        called_at = monotonic()
        try:
            result = await call_next(context)
        except BaseException as exc:
            # ⚠ Bound to a local before the build thunk below closes over it.
            # Python DELETES the `except ... as exc` name at the end of the
            # block, so a lambda capturing `exc` reads a name that is gone the
            # moment the thunk outlives the handler — and a thunk is exactly
            # what `safe_emit` takes. Harmless today because `safe_emit` calls
            # it synchronously inside this block; this makes it not depend on
            # that.
            raised = exc
            vendor_exc = vendor_exception(raised, "fastmcp")
            duration_ms = int((monotonic() - called_at) * 1000)
            seq_err = await next_sequence(self._counter, call_session)

            def build_error() -> ToolCallErrorEvent:
                fields = raised_error_fields(
                    mode=self._result_capture_mode,
                    scrubber=self._scrubber,
                    raised=raised,
                    tool_error=ToolError,
                )
                return ToolCallErrorEvent(
                    tenant_id=self._tenant_id,
                    vendor_id=self._vendor_id,
                    consent_token=self._consent_token,
                    session_id=call_session.id,
                    sequence_number=seq_err,
                    captured_at=datetime.now(UTC),
                    client_observed=client,
                    principal=caller.principal,
                    organization=caller.organization,
                    transport_observed=call_transport,
                    session_id_origin=call_session.origin,
                    call_id=call_id,
                    runtime_meta=scrubbed_meta,
                    payload=ToolCallErrorPayload(
                        tool_name=tool_name,
                        error_type=type(vendor_exc).__name__,
                        error_body=fields.error_body,
                        duration_ms=duration_ms,
                        result_capture=fields.result_capture,
                    ),
                )

            await safe_emit(self._sink, build_error, logger)
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

        # SPEC §11.4.3: a returned result carrying MCP's error flag is a
        # FAILURE. MCP files it as a 200, so it arrives here and not through
        # the ``except`` above — this adapter sits INSIDE fastmcp's own
        # exception-to-flag conversion for a raise, and OUTSIDE it for a
        # vendor's explicit return. Checked after the MRTR pause: a paused
        # round has not finished, so it is neither an end nor an error yet.
        #
        # ⚠ On fastmcp 2.14.7 ``ToolResult`` has no such field at all, so
        # ``is_error_result`` answers False and this leg emits an end exactly
        # as before. That is correct — there is no flag to misread — and it is
        # pinned positively by ``test_floor_has_no_flag_to_read``.
        if is_error_result(result):
            seq_err = await next_sequence(self._counter, call_session)

            # SPEC §11.4.3(2): both result-derived members, decided together.
            # ⚠ The projection runs INSIDE the thunk. It owns the scrubber call
            # (`_error_result.py`), and as a statement out here a raising vendor
            # scrubber escaped `safe_emit` and broke the vendor's tool call —
            # the guard only covers what the thunk it is handed evaluates. A
            # `def` rather than a lambda because the projection is read three
            # times. Dropping rather than degrading: `error_body` is a required
            # `str` and `""` is the withheld/no-message ambiguity §11.4.3 warns
            # about, so no value here honestly says "we could not scrub this".
            def build_err() -> ToolCallErrorEvent:
                err = returned_error_fields(
                    mode=self._result_capture_mode,
                    scrubber=self._scrubber,
                    result=result,
                )
                return ToolCallErrorEvent(
                    tenant_id=self._tenant_id,
                    vendor_id=self._vendor_id,
                    consent_token=self._consent_token,
                    session_id=call_session.id,
                    sequence_number=seq_err,
                    captured_at=datetime.now(UTC),
                    client_observed=client,
                    principal=caller.principal,
                    organization=caller.organization,
                    transport_observed=call_transport,
                    session_id_origin=call_session.origin,
                    call_id=call_id,
                    runtime_meta=scrubbed_meta,
                    payload=ToolCallErrorPayload(
                        tool_name=tool_name,
                        error_type=TOOL_ERROR_TYPE,
                        duration_ms=duration_ms,
                        error_body=err.error_body,
                        result=err.result,
                        result_capture=err.result_capture,
                    ),
                )

            await safe_emit(self._sink, build_err, logger)
            return result

        seq_end = await next_sequence(self._counter, call_session)

        # ⚠ The projection runs INSIDE the thunk, same rule as the error leg
        # above: it owns the scrubber call (`_result_capture.py`), and out here
        # a raising vendor scrubber escaped `safe_emit` and broke the vendor's
        # tool call.
        def build_end() -> ToolCallEndEvent:
            end = end_result_fields(
                mode=self._result_capture_mode,
                scrubber=self._scrubber,
                to_jsonable=self._result_to_jsonable,
                result=result,
            )
            return ToolCallEndEvent(
                tenant_id=self._tenant_id,
                vendor_id=self._vendor_id,
                consent_token=self._consent_token,
                session_id=call_session.id,
                sequence_number=seq_end,
                captured_at=datetime.now(UTC),
                client_observed=client,
                principal=caller.principal,
                organization=caller.organization,
                transport_observed=call_transport,
                session_id_origin=call_session.origin,
                call_id=call_id,
                runtime_meta=scrubbed_meta,
                payload=ToolCallEndPayload(
                    tool_name=tool_name,
                    duration_ms=duration_ms,
                    result=end.result,
                    result_capture=end.result_capture,
                ),
            )

        await safe_emit(self._sink, build_end, logger)
        return result

    # =========================================================================
    # Internal — extraction helpers
    # =========================================================================

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
