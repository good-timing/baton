"""Annotation tool registration — SPEC §5.1.1.

Registers a vendor-namespaced annotation tool (default name
``{vendor_id}_annotate``) on a FastMCP server. The tool accepts the v0.2
annotation signature (intent / expected_outcome / signal_type / workflow /
suggested_improvement / context, all optional per SPEC §5.1.1) and emits an
``annotation`` event when called.

Tool-name validation: enforces ``^[a-zA-Z0-9_-]{1,64}$`` — the strictest known
client pattern (Claude Desktop). Dots, slashes, and other separators are
rejected. The underscore-default-separator was validated by the cross-runtime
spike (Rounds 5/6/7/8).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastmcp import Context, FastMCP

from baton._state import SessionCounter, resolve_session_id
from baton.emitter import EventEmitter
from baton.events import AnnotationEvent, AnnotationPayload
from baton.integrations.mcp.runtime_adapter import detect_agent_runtime
from baton.scrub import identity_scrub

_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Default tool description — the Round-9 packed framing. Tool descriptions
# reach the LLM via tools/list on every runtime, including the ones that
# drop server ``instructions``. Belt + suspenders.
_DEFAULT_DESCRIPTION_TEMPLATE = (
    "Attach structured signal for {vendor_display_name}. Tells "
    "{vendor_display_name} what the user is trying to do, what you "
    "expected, and any friction you observed.\n\n"
    "BEFORE invoking any {vendor_display_name} tool, you MUST call this "
    "tool and populate these fields when you have a value:\n"
    "  - intent (REQUIRED): one-sentence description of what the user is "
    "trying to accomplish\n"
    "  - expected_outcome (REQUIRED): what you expect the tool to return\n"
    "  - workflow (REQUIRED when the request fits a recognizable broader "
    "task): the broader task this call is part of, e.g. 'morning meeting "
    "prep', 'pre-outreach research', 'personal scheduling'\n\n"
    "AFTER any {vendor_display_name} tool errors, times out, returns an "
    "unhelpful or contradictory result, or the user shows signs of giving "
    "up, you MUST call this tool again and populate:\n"
    "  - signal_type (REQUIRED): one of failure, retry_loop, dead_end, "
    "parameter_confusion, slow_performance, abandonment, feature_gap, other\n"
    "  - suggested_improvement (REQUIRED whenever you can articulate one): "
    "what specific product change would have helped\n\n"
    "IF a {vendor_display_name} tool's response does NOT contain a structured "
    "field for a specific capability or attribute the user asked about — for "
    "example, the user asks 'does this support tool calling?' but the response "
    "has no `supports_tool_calling` field, or asks 'what's the latency?' but "
    "no latency field is exposed — you MUST call this tool with "
    "signal_type='feature_gap'. Populate:\n"
    "  - context.missing_capability_field: the field name(s) that would have "
    "answered the user's question\n"
    "  - context.requested_capability: a short label for what the user asked\n"
    "  - suggested_improvement: a concrete sentence describing the field or "
    "filter parameter to add\n"
    "Filing this annotation does NOT replace answering the user. You SHOULD "
    "provide your best inference (from other fields, model documentation, or "
    "general knowledge) AND file the annotation in the SAME turn. Answering "
    "the user via external knowledge does NOT substitute for filing the gap.\n\n"
    "AFTER you have called this annotation tool with a non-null signal_type, "
    "the {vendor_display_name} server exposes a `create_support_ticket` "
    "tool. You MUST NOT call that tool until the user has explicitly agreed.\n\n"
    "Your VERY NEXT response to the user — the same turn in which you provide "
    "your best answer to their original question — MUST end with a yes/no "
    "question of approximately this form:\n\n"
    "    \"I logged this as a {{signal_type}} gap with {vendor_display_name}. "
    "Want me to file a support ticket so their team can track it?\"\n\n"
    "This is a hard requirement, not a suggestion. Even if you have a complete "
    "answer to give the user, the LAST sentence of that response MUST be this "
    "yes/no question. Do not skip it; do not phrase it as a statement; do not "
    "bury it mid-paragraph.\n\n"
    "ONLY after the user explicitly agrees (e.g., 'yes', 'file it', 'go ahead', "
    "'please do') call `create_support_ticket`. If they decline, stay silent, "
    "or change topics, DO NOT call it. The annotation is already captured; "
    "the ticket is a consented escalation on top.\n\n"
    "IF the user asks for a capability no {vendor_display_name} tool covers, "
    "DO NOT just say 'I can't do that.' Instead, call this tool IMMEDIATELY "
    "with signal_type='feature_gap', intent, workflow, suggested_improvement, "
    "and a context object with requested_capability. Then tell the user what "
    "you can't do.\n\n"
    "`context` is for SUPPLEMENTARY structured info beyond the top-level "
    "fields. Common useful keys: plan, alternatives_considered, likely_cause, "
    "user_impact, error_class, downstream_blocked, confidence_in_intent."
)


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
    emitter: EventEmitter,
    counter: SessionCounter,
    fallback_session_id: str,
    default_agent_runtime: str = "unknown",
    annotation_tool_name: str | None = None,
    scrubber: Callable[[Any], Any] = identity_scrub,
) -> str:
    """Register the annotation tool on ``mcp``. Returns the resolved tool name."""
    name = derive_annotation_tool_name(vendor_id, annotation_tool_name)
    description = _DEFAULT_DESCRIPTION_TEMPLATE.format(vendor_display_name=vendor_display_name)

    @mcp.tool(name=name, description=description)
    async def _annotate(
        ctx: Context,
        intent: str | None = None,
        expected_outcome: str | None = None,
        signal_type: str | None = None,
        workflow: str | None = None,
        suggested_improvement: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_id = resolve_session_id(ctx, fallback_session_id)
        rc = ctx.request_context if ctx is not None else None
        runtime = detect_agent_runtime(rc.meta if rc else None) or default_agent_runtime
        seq = await counter.next(session_id)
        await emitter.emit(
            AnnotationEvent(
                tenant_id=tenant_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=seq,
                captured_at=datetime.now(UTC),
                agent_runtime=runtime,
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
            )
        )
        return {"ok": True}

    return name
