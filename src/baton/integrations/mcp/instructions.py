"""Server-instructions template for the FastMCP ``instructions`` field.

The MCP spec's ``InitializeResult`` carries a server-supplied ``instructions``
string that compliant clients SHOULD surface to the calling LLM. Empirically
load-bearing in Claude Code + Cursor; ignored by Claude Desktop and the
Claude.ai web MCP connector (Anthropic's own docs say "only tool calls are
currently supported" on the consumer surfaces).

Two framing decisions are load-bearing:
  1. Top-level fields use MUST + (REQUIRED) markers — milder framing
     under-populates them.
  2. ``context`` is framed positively as "supplementary" — anti-duplication
     warnings backfire.
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
    "AFTER you have called `{annotation_tool_name}` with a non-null "
    "signal_type, the {vendor_display_name} server exposes a "
    "`create_support_ticket` tool. You MUST NOT call this tool until the user "
    "has explicitly agreed.\n\n"
    "Your VERY NEXT response to the user — the same turn in which you provide "
    "your best answer to their original question — MUST end with a yes/no "
    "question of approximately this form:\n\n"
    '    "I logged this as a {{signal_type}} gap with {vendor_display_name}. '
    'Want me to file a support ticket so their team can track it?"\n\n'
    "This is a hard requirement, not a suggestion. Even if you have a complete "
    "answer to give the user, the LAST sentence of that response MUST be this "
    "yes/no question. Do not skip it; do not phrase it as a statement; do not "
    "bury it mid-paragraph.\n\n"
    "ONLY after the user explicitly agrees (e.g., 'yes', 'file it', 'go ahead', "
    "'please do') call `create_support_ticket`. If they decline, stay silent, "
    "or change topics, DO NOT call it. The annotation is already captured; "
    "the ticket is a consented escalation on top.\n\n"
    "IF the user asks for a capability that no {vendor_display_name} tool covers, "
    "DO NOT just say 'I can't do that.' Instead, call `{annotation_tool_name}` "
    "IMMEDIATELY with signal_type='feature_gap', intent, workflow, and "
    "suggested_improvement. Then tell the user what you can't do.\n\n"
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
