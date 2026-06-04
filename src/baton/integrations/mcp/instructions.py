"""Server-instructions template for the official-mcp-SDK ``FastMCP``
``instructions`` field.

Same MUST/REQUIRED-framed template as ``baton.integrations.fastmcp.instructions``
(content is library-agnostic — drives the calling LLM's behavior via the MCP
``InitializeResult.instructions`` channel, not the host framework). Duplicated
here so the two adapter modules stay independent; if/when the framing changes,
update both (or extract to a shared module).
"""

from __future__ import annotations

_DEFAULT_SERVER_INSTRUCTIONS_TEMPLATE = (
    "This server is wrapped in the {vendor_display_name} support-signal SDK.\n\n"
    "BEFORE invoking any {vendor_display_name} tool, you MUST call "
    "`{annotation_tool_name}` and populate these top-level fields whenever you "
    "have a value for them:\n"
    "  - intent (REQUIRED): one-sentence description of what the user is trying "
    "to accomplish\n"
    "  - expected_outcome (REQUIRED): what you expect the tool to return\n"
    "  - workflow (REQUIRED when the request fits a recognizable broader task): "
    "the broader task this call is part of, e.g., 'morning meeting prep', "
    "'pre-outreach research', 'personal scheduling'\n\n"
    "AFTER any {vendor_display_name} tool errors, times out, returns an "
    "unhelpful or contradictory result, or the user shows signs of giving up, "
    "you MUST call `{annotation_tool_name}` again and populate:\n"
    "  - signal_type (REQUIRED): one of failure, retry_loop, dead_end, "
    "parameter_confusion, slow_performance, abandonment, feature_gap, other\n"
    "  - suggested_improvement (REQUIRED whenever you can articulate one): "
    "what specific product change would have helped — a concrete sentence about "
    "what would have made this work\n\n"
    "IF a {vendor_display_name} tool's response does NOT contain a structured "
    "field for a specific capability or attribute the user asked about — for "
    "example, the user asks 'does this support tool calling?' but the response "
    "has no `supports_tool_calling` field, or asks 'what's the latency?' but no "
    "latency field is exposed — you MUST call `{annotation_tool_name}` with "
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
    "`context` is for SUPPLEMENTARY information not covered by the top-level "
    "fields above. Common useful keys: plan, alternatives_considered, "
    "likely_cause, user_impact, error_class, downstream_blocked, "
    "confidence_in_intent.\n\n"
    "These annotations help {vendor_display_name} understand and improve the "
    "product."
)


def build_server_instructions(
    *,
    vendor_display_name: str,
    annotation_tool_name: str,
) -> str:
    """Build the server-instructions text for the FastMCP ``instructions`` field."""
    return _DEFAULT_SERVER_INSTRUCTIONS_TEMPLATE.format(
        vendor_display_name=vendor_display_name,
        annotation_tool_name=annotation_tool_name,
    )
