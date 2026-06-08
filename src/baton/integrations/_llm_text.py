"""Shared LLM-facing text — server instructions + annotation tool description.

Two adapters (``baton.integrations.fastmcp``, ``baton.integrations.mcp``)
surface identical text to the calling agent; this module owns the canonical
copy so they cannot drift.

**Split of responsibility (load-bearing for §5.1.2 under Claude Code's
truncation cap):**

- *Server instructions* carry the MUST/REQUIRED behavioral framing
  (BEFORE/AFTER pattern, signal_type enum, "annotation doesn't replace
  answering"). Claude Code truncates ``InitializeResult.instructions`` at
  ~2087 chars, so this template is kept under ~1000 chars to leave headroom
  for vendor extensions (``BatonExtension.instructions_slice``).
- *Annotation tool description* carries the field-level reference (what
  belongs in intent / expected_outcome / workflow / suggested_improvement /
  context) PLUS any ``BatonExtension.description_directive`` contributions.
  Tool descriptions are loaded on every call to the central annotation tool,
  so this is the right place for the just-in-time dictionary and any
  condition-action directives that must fire at annotation time.

Why not put both in instructions: empirically the cap drops the tail
silently. Why not put the behavioral framing in the description: per-call
context overhead, and the description is read at *call* time — too late to
drive the first proactive annotation.
"""

from __future__ import annotations

import logging

_DEFAULT_SERVER_INSTRUCTIONS_TEMPLATE = (
    "This server is wrapped in the {vendor_display_name} support-signal SDK. "
    "Use `{annotation_tool_name}` to record what the user is trying to do and "
    "how each {vendor_display_name} tool call went. See that tool's "
    "description for field-level detail.\n\n"
    "BEFORE invoking any {vendor_display_name} tool, you MUST call "
    "`{annotation_tool_name}` with intent (REQUIRED), expected_outcome "
    "(REQUIRED), and workflow (REQUIRED when the request fits a recognizable "
    "broader task, e.g., 'morning meeting prep', 'pre-outreach research').\n\n"
    "AFTER any {vendor_display_name} tool errors, times out, returns an "
    "unhelpful or contradictory result, or the user shows signs of giving "
    "up, you MUST call `{annotation_tool_name}` again with signal_type "
    "(REQUIRED) — one of failure, retry_loop, dead_end, parameter_confusion, "
    "slow_performance, abandonment, feature_gap, other — and "
    "suggested_improvement (REQUIRED whenever you can articulate one).\n\n"
    "IF a {vendor_display_name} tool response lacks a structured field for "
    "what the user asked about, you MUST call `{annotation_tool_name}` with "
    "signal_type='feature_gap' AND still answer the user with your best "
    "inference. Filing the annotation does NOT replace answering."
)


_DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE = (
    "Record structured signal about a {vendor_display_name} tool call — "
    "what the user is trying to do, and how it went. Populate proactively "
    "before the call (intent + expected_outcome + workflow) and reactively "
    "after if the result was unhelpful (signal_type + suggested_improvement).\n"
    "\n"
    "Fields:\n"
    "  - intent: one sentence on what the user is trying to accomplish.\n"
    "  - expected_outcome: what you expect the tool to return.\n"
    "  - workflow: the broader task this call is part of, e.g., 'morning "
    "meeting prep', 'pre-outreach research', 'personal scheduling'. Skip "
    "when the call doesn't fit a recognizable broader task.\n"
    "  - signal_type: one of failure, retry_loop, dead_end, "
    "parameter_confusion, slow_performance, abandonment, feature_gap, other.\n"
    "  - suggested_improvement: a concrete sentence about what product "
    "change would have helped.\n"
    "  - context: supplementary info not covered above. Common keys: plan, "
    "alternatives_considered, likely_cause, user_impact, error_class, "
    "downstream_blocked, confidence_in_intent. For signal_type='feature_gap' "
    "also missing_capability_field and requested_capability."
)


_INSTRUCTIONS_SOFT_CAP = 2000
"""Soft warning threshold for combined server-instructions length.

Claude Code empirically truncates at ~2087 chars. We warn at 2000 to leave
a safety margin; the base template uses ~1.1K chars, leaving ~900 chars for
``BatonExtension.instructions_slice`` contributions.
"""

_log = logging.getLogger(__name__)


def build_server_instructions(
    *,
    vendor_display_name: str,
    annotation_tool_name: str,
    extra_slices: list[str] | None = None,
) -> str:
    """Build the server-instructions text for the MCP ``instructions`` field.

    ``extra_slices``: text contributed by ``BatonExtension.instructions_slice``
    implementations. Each slice is appended with a blank-line separator.
    Emits a ``logging.WARNING`` if the combined length exceeds
    ``_INSTRUCTIONS_SOFT_CAP`` — prefer ``BatonExtension.description_directive``
    for behavioural guidance that has no truncation risk.
    """
    base = _DEFAULT_SERVER_INSTRUCTIONS_TEMPLATE.format(
        vendor_display_name=vendor_display_name,
        annotation_tool_name=annotation_tool_name,
    )
    if not extra_slices:
        return base
    combined = base + "".join(f"\n\n{s}" for s in extra_slices)
    if len(combined) > _INSTRUCTIONS_SOFT_CAP:
        _log.warning(
            "build_server_instructions: combined instructions length %d chars "
            "exceeds soft cap %d — Claude Code truncates at ~2087; "
            "BatonExtension.instructions_slice contributions may be cut off. "
            "Use BatonExtension.description_directive instead for directives "
            "that must reach the agent reliably.",
            len(combined),
            _INSTRUCTIONS_SOFT_CAP,
        )
    return combined


def build_annotation_tool_description(
    *,
    vendor_display_name: str,
    extra_directives: list[str] | None = None,
) -> str:
    """Build the annotation tool's ``description``.

    ``extra_directives``: condition-action directives contributed by
    ``BatonExtension.description_directive`` implementations. Each directive
    is appended as its own paragraph. The annotation tool description is
    loaded on every call (not subject to the server-instructions truncation
    cap), making this the strongest reliable channel for behavioural guidance.
    """
    base = _DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE.format(
        vendor_display_name=vendor_display_name,
    )
    if not extra_directives:
        return base
    return base + "\n\n" + "\n\n".join(extra_directives)
