"""Annotation tool registration — SPEC §5.1.1 — for the official mcp SDK's
``mcp.server.fastmcp.FastMCP``.

Mirrors ``baton.integrations.standalone.annotation`` but registers via the
official SDK's ``@mcp.tool(...)`` decorator.

**Note on `from __future__ import annotations` (intentionally omitted):**
mcp <=1.20 introspects tool signatures via ``inspect.signature(fn)`` and
does ``issubclass(param.annotation, Context)`` to detect Context kwargs.
That ``issubclass`` would crash on stringified annotations (which is what
``from __future__ import annotations`` produces). Keeping annotations as
live types lets ``get_origin`` correctly identify union/generic types and
skip the ``issubclass`` call. Required for mcp 1.10/1.20 support per
CHANGELOG 0.2.0 + CI matrix.
"""

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from baton._state import ProactiveTracker, SessionCounter
from baton.events import AnnotationEvent, AnnotationPayload
from baton.integrations._llm_text import build_annotation_tool_description
from baton.integrations.identity_adapter import USER_ID_MODE_HASHED, resolve_user_id
from baton.integrations.official import _auth
from baton.integrations.official._compat import ContextClass as Context
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.integrations.official._tool_wrap import _extract_meta_from_context
from baton.integrations.runtime_adapter import UNKNOWN_AGENT_RUNTIME, detect_agent_runtime
from baton.scrub import identity_scrub
from baton.sinks import Sink, safe_write

logger = logging.getLogger(__name__)

_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def derive_annotation_tool_name(vendor_id: str, override: str | None = None) -> str:
    """Resolve the annotation tool name. Default is ``{vendor_id}_annotate``.

    Raises ``ValueError`` if the resulting name violates the strict
    cross-runtime client pattern.
    """
    name = override or f"{vendor_id}_annotate"
    if not _TOOL_NAME_PATTERN.match(name):
        raise ValueError(
            f"Annotation tool name {name!r} violates the cross-runtime "
            f"pattern {_TOOL_NAME_PATTERN.pattern!r} (Claude Desktop and others "
            f"reject names with dots or other separators)."
        )
    return name


def register_annotation_tool(
    mcp: FastMCP,
    *,
    vendor_id: str,
    vendor_display_name: str,
    tenant_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    fallback_session_id: str,
    annotation_tool_name: str | None = None,
    proactive_mode: str = "off",
    scrubber: Callable[[Any], Any] = identity_scrub,
    proactive_tracker: ProactiveTracker | None = None,
    user_id_mode: str = USER_ID_MODE_HASHED,
    user_id_hmac_key: bytes | None = None,
    identity_warned: set[str] | None = None,
) -> str:
    """Register the annotation tool on ``mcp``. Returns the resolved tool name."""
    tracker = proactive_tracker or ProactiveTracker()
    warned = identity_warned if identity_warned is not None else set()
    name = derive_annotation_tool_name(vendor_id, annotation_tool_name)
    description = build_annotation_tool_description(
        vendor_display_name=vendor_display_name, proactive_mode=proactive_mode
    )

    # ``mcp`` is ``Any`` under type-checking (see ``_compat``: the server class
    # is resolved per installed mcp major, so neither concrete class is right
    # for both), which makes this decorator untyped to mypy under EVERY mcp
    # version rather than only some — so this suppression is stable instead of
    # flipping to `unused-ignore` on the next upstream major.
    @mcp.tool(name=name, description=description)  # type: ignore[untyped-decorator]
    async def _annotate(
        user_goal: str,
        expected_result: str | None = None,
        signal_type: str | None = None,
        overall_task: str | None = None,
        suggested_improvement: str | None = None,
        context: dict[str, Any] | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        # proactive_mode="off": refuse pre-call annotations structurally rather
        # than by instruction text alone. Text alone is only a request, and a
        # single umbrella `overall_task` label from one stray proactive is
        # enough to merge distinct tasks in any consumer that keys grouping
        # on it (the annotation label outranks the per-call one there).
        # Rejecting here, rather than requiring signal_type in the schema,
        # keeps the agent from fabricating a `failure` just to get the call
        # through — that would corrupt the reactive signal, which is the one
        # worth protecting.
        if proactive_mode == "off" and signal_type is None:
            return {
                "ok": False,
                "error": (
                    f"{name} is reactive-only on this server. Call it only AFTER "
                    "a tool call returns an unhelpful, empty, failed or "
                    "contradictory result, or when no tool covers what the user "
                    "asked for — and set signal_type. What the user is trying to "
                    "do is already recorded on each tool call, so no pre-call "
                    "annotation is needed."
                ),
            }

        # The MCP Context is threaded in for its ``_meta``, so this event
        # reports the SAME agent_runtime as the tool calls around it. Without
        # it this tool emitted the install-time default unconditionally, which
        # made an annotation and the call it describes disagree about who was
        # calling.
        #
        # It is annotated as a BARE ``Context`` on purpose. The older objection
        # here was that mcp's ``Tool.from_function`` calls
        # ``issubclass(param.annotation, Context)`` on each kwarg, which
        # crashes on a PARAMETERIZED generic like ``Context[Any, Any, Any]``.
        # Unparameterized it is a plain class and ``issubclass`` is fine —
        # measured registering cleanly on mcp 1.20.0 (the floor) and 2.0.0,
        # with ``context_kwarg`` detected so it stays OUT of the tool's public
        # schema. It is also named ``ctx`` rather than ``context`` because
        # ``context`` is already this tool's own payload field.
        #
        # Still a known gap, and now the ONLY one: session id. This tool does
        # not climb SPEC §3.4's ladder or check VendorConfig.resolve_session_id
        # the way _tool_wrap.py's rung 0 does — it still falls back. So a
        # vendor's explicit (reactive) annotation calls on this adapter won't
        # stitch to the hook-resolved session id their tool calls get;
        # synthesised proactives are unaffected (those emit from inside the
        # wrap layer, which does have the hook). The standalone adapter
        # resolves both; closing the difference is tracked on sdk-hardening.
        # Reuses the wrap layer's extractor rather than re-deriving the meta
        # here. Two extraction paths on one adapter is how this repo has been
        # bitten before, and this one has a specific guard worth inheriting:
        # ``ctx.request_context`` RAISES ``ValueError`` outside a live request
        # (a tool invoked programmatically, which every test here does), so a
        # plain ``getattr(ctx, "request_context", None)`` does not save you —
        # getattr's default only swallows AttributeError.
        meta_dict = _extract_meta_from_context(ctx)
        # Detect from the RAW meta — the scrubber runs on the values below, and
        # a vendor scrubber that touches meta keys must not be able to turn
        # runtime detection off. Same rule as both tool-call paths.
        runtime = detect_agent_runtime(meta_dict, context=ctx, scrubber=scrubber) or (
            UNKNOWN_AGENT_RUNTIME
        )
        # The meta is read for the RUNTIME and deliberately not emitted as
        # ``runtime_meta`` on this event, unlike the tool-call path. It can
        # carry ``io.baton/session_id`` and ``traceparent`` while ``session_id``
        # below is still the fallback, so emitting both would put a session
        # identifier on an event whose own envelope field disagrees with it —
        # one consumer grouping on the forwarded value and another reading the
        # envelope would file this single event under two sessions. Today's gap
        # only LOSES a join; that would manufacture a wrong one.
        #
        # ⚠ Retiring §3.4's rungs 1-2 (2026-09-09) does NOT dissolve this. It
        # is tempting to think it does — the SDK no longer treats either key as
        # session-bearing — but the retirement moved that grouping DOWNSTREAM
        # rather than abolishing it, so a console that groups on a forwarded
        # handle is now the expected consumer, not a hypothetical one. The
        # hazard is unchanged; only its address moved.
        #
        # Emitting it becomes correct as soon as this tool resolves a real
        # session id instead of the fallback — unblocked, since the ``ctx``
        # that resolution needs is finally threaded in.
        # Identity, on the same terms as the tool-call path: the finished
        # wire value, resolved once, raw principal never travelling past it.
        # Unlike ``runtime_meta`` just above, there is no asymmetry argument
        # against emitting this one — ``user_id`` carries no session identity,
        # so it cannot disagree with the envelope's ``session_id`` the way a
        # forwarded meta key can.
        annotation_user_id = resolve_user_id(
            _auth.current_access_token(),
            mode=user_id_mode,
            tenant_id=tenant_id,
            hmac_key=user_id_hmac_key,
            logger=logger,
            warned=warned,
        )
        session_id = fallback_session_id
        # A proactive annotation (no signal_type) claims the session's proactive
        # slot so the wrap layer won't also synthesise one from an injected param.
        if signal_type is None:
            tracker.mark(session_id)
        seq = await counter.next(session_id)
        await safe_write(
            sink,
            AnnotationEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=seq,
                captured_at=datetime.now(UTC),
                agent_runtime=runtime,
                user_id=annotation_user_id,
                payload=AnnotationPayload(
                    intent=scrubber(user_goal) if user_goal else None,
                    expected_outcome=(scrubber(expected_result) if expected_result else None),
                    signal_type=signal_type,
                    # Agent-facing param `overall_task` -> wire key `workflow`, the
                    # same split the injected params use (`overall_task` ->
                    # `call_workflow`): renaming the param must not move the key the
                    # console groups on.
                    workflow=overall_task,
                    suggested_improvement=(
                        scrubber(suggested_improvement) if suggested_improvement else None
                    ),
                    context=scrubber(context) if context else None,
                ),
            ),
            logger,
        )
        return {"ok": True}

    return name
