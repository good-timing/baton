"""Annotation tool registration — SPEC §5.1.1.

Registers a vendor-namespaced annotation tool (default name
``{vendor_id}_annotate``) on a FastMCP server. The tool accepts the
annotation signature (intent / expected_outcome / signal_type / workflow /
suggested_improvement / context, all optional per SPEC §5.1.1) and emits an
``annotation`` event when called.

Tool-name validation: enforces ``^[a-zA-Z0-9_-]{1,64}$`` — the strictest known
client pattern (Claude Desktop). Dots, slashes, and other separators are
rejected. The underscore-default-separator was validated by the cross-runtime
spike (Rounds 5/6/7/8).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers

from baton._state import ProactiveTracker, SessionCounter, resolve_session_id
from baton.events import AnnotationEvent, AnnotationPayload
from baton.integrations._config import (
    ResolveSessionIdHook,
    SessionResolutionContext,
    resolve_via_hook,
)
from baton.integrations._llm_text import build_annotation_tool_description
from baton.integrations.fastmcp.runtime_adapter import detect_agent_runtime, meta_to_dict
from baton.scrub import identity_scrub
from baton.sinks import Sink, safe_write

logger = logging.getLogger(__name__)

_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def derive_annotation_tool_name(vendor_id: str, override: str | None = None) -> str:
    """Resolve the annotation tool name. Default is ``{vendor_id}_annotate``;
    vendors MAY supply ``override`` to use a different name.

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
    default_agent_runtime: str = "unknown",
    annotation_tool_name: str | None = None,
    scrubber: Callable[[Any], Any] = identity_scrub,
    proactive_tracker: ProactiveTracker | None = None,
    resolve_session_id_hook: ResolveSessionIdHook | None = None,
) -> str:
    """Register the annotation tool on ``mcp``. Returns the resolved tool name."""
    tracker = proactive_tracker or ProactiveTracker()
    name = derive_annotation_tool_name(vendor_id, annotation_tool_name)
    description = build_annotation_tool_description(vendor_display_name=vendor_display_name)

    @mcp.tool(name=name, description=description)
    async def _annotate(
        ctx: Context,
        intent: str,
        expected_outcome: str | None = None,
        signal_type: str | None = None,
        workflow: str | None = None,
        suggested_improvement: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rc = ctx.request_context if ctx is not None else None
        raw_meta = rc.meta if rc else None
        meta_dict = meta_to_dict(raw_meta)
        runtime = detect_agent_runtime(raw_meta) or default_agent_runtime
        scrubbed_meta = scrubber(meta_dict) if meta_dict is not None else None

        # Rung 0 (a configured VendorConfig.resolve_session_id hook), same
        # priority as the middleware's tool-call path — see design note
        # docs/design-notes/session_resolver_hook.md. Falls back to FastMCP's
        # own Context.session_id, same as before this hook existed.
        session_id: str | None = None
        if resolve_session_id_hook is not None:
            headers = get_http_headers(include_all=True) or None
            session_id = await resolve_via_hook(
                resolve_session_id_hook,
                SessionResolutionContext(
                    headers=headers,
                    meta=meta_dict,
                    tool_name=name,
                    arguments={
                        "intent": intent,
                        "expected_outcome": expected_outcome,
                        "signal_type": signal_type,
                        "workflow": workflow,
                        "suggested_improvement": suggested_improvement,
                        "context": context,
                    },
                ),
            )
        if session_id is None:
            session_id = resolve_session_id(ctx, fallback_session_id)
        # A proactive annotation (no signal_type) claims the session's proactive
        # slot so the middleware won't also synthesise one from an injected param.
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
                runtime_meta=scrubbed_meta,
                payload=AnnotationPayload(
                    intent=scrubber(intent) if intent else None,
                    expected_outcome=(scrubber(expected_outcome) if expected_outcome else None),
                    signal_type=signal_type,
                    workflow=workflow,
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
