"""Annotation tool registration — SPEC §5.1.1.

Registers a vendor-namespaced annotation tool on a FastMCP server — named
after the server itself by default, falling back to ``{vendor_id}_annotate``
(see ``integrations._annotation_name``). The tool accepts the
annotation signature (intent / expected_outcome / what_happened / tool_name / overall_task /
suggested_improvement / context, all optional per SPEC §5.1.1) and emits an
``annotation`` event when called.

Tool-name validation: enforces ``^[a-zA-Z0-9_-]{1,64}$`` — the strictest known
client pattern (Claude Desktop). Dots, slashes, and other separators are
rejected. The underscore-default-separator was validated by the cross-runtime
spike (Rounds 5/6/7/8).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastmcp import Context, FastMCP

from baton._meta_coords import round_meta_coordinates
from baton._state import ProactiveTracker, SessionCounter
from baton.events import AnnotationEvent
from baton.integrations._annotation_name import derive_annotation_tool_name
from baton.integrations._annotation_payload import build_annotation_payload, is_report
from baton.integrations._config import SessionResolutionContext
from baton.integrations._llm_text import build_annotation_tool_description
from baton.integrations.client_observed import meta_to_dict, observe_client
from baton.integrations.identity_adapter import (
    ResolvePrincipalHook,
    resolve_call_principal,
    token_claims,
)
from baton.integrations.standalone import _auth
from baton.integrations.standalone._session import (
    extract_headers,
    observe_transport,
    resolve_call_session_id,
)
from baton.scrub import identity_scrub, scrub_or_none
from baton.sinks import Sink, safe_emit

logger = logging.getLogger(__name__)

#: ``derive_annotation_tool_name`` is RE-EXPORTED here, not merely imported.
#: Its body moved to ``_annotation_name`` (it was byte-identical in both
#: adapters), but this path stays live API: ``baton-console``'s tests import it
#: as ``from baton.integrations.standalone.annotation import
#: derive_annotation_tool_name``, and the pre-rename shims cite it in their
#: docstrings.
#:
#: ⚠ Declared in ``__all__`` because ruff's F401 autofix DELETED the bare
#: import during this refactor, the whole suite stayed green, and the break
#: would only have surfaced in the other repo. ``test_the_console_imports_it_
#: from_the_adapter_path`` now pins it.
__all__ = ["derive_annotation_tool_name", "register_annotation_tool"]


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
    annotation_tool_name: str,
    proactive_mode: str = "off",
    scrubber: Callable[[Any], Any] = identity_scrub,
    proactive_tracker: ProactiveTracker | None = None,
    resolve_principal_hook: ResolvePrincipalHook | None = None,
) -> str:
    """Register the annotation tool on ``mcp``. Returns the resolved tool name."""
    tracker = proactive_tracker or ProactiveTracker()
    name = annotation_tool_name
    description = build_annotation_tool_description(
        vendor_display_name=vendor_display_name, proactive_mode=proactive_mode
    )

    @mcp.tool(name=name, description=description)
    async def _annotate(
        ctx: Context,
        user_goal: str,
        expected_result: str | None = None,
        what_happened: str | None = None,
        tool_name: str | None = None,
        overall_task: str | None = None,
        suggested_improvement: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # proactive_mode="off": refuse pre-call annotations structurally rather
        # than by instruction text alone. Text alone is only a request, and a
        # single umbrella `overall_task` label from one stray proactive is
        # enough to merge distinct tasks in any consumer that keys grouping
        # on it (the annotation label outranks the per-call one there).
        # Rejecting here, rather than requiring what_happened in the schema,
        # keeps the agent from inventing a problem just to get the call
        # through — that would corrupt the reports, which are the signal
        # worth protecting.
        reporting = is_report(what_happened)
        if proactive_mode == "off" and not reporting:
            return {
                "ok": False,
                "error": (
                    f"{name} is reactive-only on this server. Call it only AFTER "
                    "a tool call returns an unhelpful, empty, failed or "
                    "contradictory result, or when no tool covers what the user "
                    "asked for — and say what_happened. What the user is trying to "
                    "do is already recorded on each tool call, so no pre-call "
                    "annotation is needed."
                ),
            }

        rc = ctx.request_context if ctx is not None else None
        meta_dict = meta_to_dict(rc.meta if rc else None)
        call_headers = extract_headers()
        client = observe_client(meta_dict, context=ctx, headers=call_headers, scrubber=scrubber)
        # Coordinates round before the vendor's scrubber, whatever it is
        # (``_meta_coords``).
        #
        # scrub_or_none, not a bare call: this is a plain statement OUTSIDE the
        # build thunk below, and it stays out there deliberately — it must keep
        # scrubbing BEFORE the awaits that follow, one of which runs the
        # vendor's own `resolve_principal` hook. `None` loses `runtime_meta`
        # alone, which costs a join and degrades this surface to what
        # `official/annotation.py` already emits.
        scrubbed_meta = scrub_or_none(
            scrubber,
            round_meta_coordinates(meta_dict) if meta_dict is not None else None,
            "annotation _meta",
            logger,
        )

        # SPEC §3.4's ladder, resolved by the SAME function the middleware's
        # tool-call path uses — an annotation that resolved differently from
        # the call it describes could never be joined to it downstream, which
        # is the one correlation this tool exists to produce.
        session_id = await resolve_call_session_id(
            headers=call_headers, fallback=fallback_session_id
        )
        # A proactive annotation (not a report) claims the session's proactive
        # slot so the middleware won't also synthesise one from an injected param.
        if not reporting:
            tracker.mark(session_id)
        seq = await counter.next(session_id)
        # Identity takes the SAME ladder the tool-call path takes, hook
        # included; see ``official/annotation.py`` for why the annotation path
        # must consult it too.
        identity_hook_context = (
            SessionResolutionContext(
                headers=call_headers,
                meta=meta_dict,
                tool_name=name,
                arguments={},
                claims=token_claims(_auth.current_access_token()),
            )
            if resolve_principal_hook is not None
            else None
        )
        annotation_principal = await resolve_call_principal(
            hook=resolve_principal_hook,
            hook_context=identity_hook_context,
            logger=logger,
        )
        # safe_emit, not safe_write: `build_annotation_payload` runs the
        # vendor's scrubber on five fields, and as an ARGUMENT expression it
        # would be evaluated in THIS frame, before `safe_write` was entered —
        # so a raising vendor scrubber used to break `_annotate` —
        # a tool on the VENDOR's server, so the vendor's end user sees their
        # server erroring. Inside the thunk a throw drops the event instead,
        # which is right here: `intent`, `what_happened` and
        # `suggested_improvement` are one report and degrade together.
        await safe_emit(
            sink,
            lambda: AnnotationEvent(
                tenant_id=tenant_id,
                vendor_id=vendor_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=seq,
                captured_at=datetime.now(UTC),
                client_observed=client,
                principal=annotation_principal,
                transport_observed=observe_transport(),
                runtime_meta=scrubbed_meta,
                payload=build_annotation_payload(
                    scrubber,
                    user_goal=user_goal,
                    expected_result=expected_result,
                    overall_task=overall_task,
                    suggested_improvement=suggested_improvement,
                    context=context,
                    what_happened=what_happened,
                    tool_name=tool_name,
                ),
            ),
            logger,
        )
        return {"ok": True}

    return name
