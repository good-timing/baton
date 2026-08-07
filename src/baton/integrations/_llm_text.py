"""Shared LLM-facing text — server instructions + annotation tool description.

Two adapters (``baton.integrations.fastmcp``, ``baton.integrations.mcp``)
surface identical text to the calling agent; this module owns the canonical
copy so they cannot drift.

**Split of responsibility (load-bearing for §5.1.2 under Claude Code's
truncation cap):**

- *Server instructions* carry the MUST/REQUIRED behavioral framing —
  the BEFORE/AFTER/IF triggers, the signal_type enum, and the
  "annotation doesn't replace answering" guardrail. Claude Code
  truncates ``InitializeResult.instructions`` at ~2087 chars, so this
  template is kept under ~1500 chars to leave headroom for vendor
  extensions. Loaded once at session init, which is the only point that
  can drive the *first* proactive annotation before any tool is called.
- *Annotation tool description* carries the field-level reference (what
  belongs in intent / expected_outcome / workflow / suggested_improvement /
  context). Tool descriptions are loaded on every call to the central
  annotation tool, so this is the right place for the just-in-time
  dictionary.

Why not put both in instructions: empirically the cap drops the tail
silently. Why not put the behavioral framing in the description: per-call
context overhead, and the description is read at *call* time — too late to
drive the first proactive annotation.

**Trigger discipline.** A live-Claude proxy test on 2026-06-12 surfaced
an asymmetry the original templates baked in: only the "if a call
returned an error" trigger was mechanical (an observable state Claude
could check at the end of any tool call); the feature-gap path
required vigilance, and vigilance loses to task completion every time.
Three mechanical triggers now sit alongside each other in the IF
block: (1) tool response lacks a structured field for what the user
asked about, (2) intent satisfied via workaround because no tool
matched, (3) user asked for something this server can't do. Each is a
state Claude can check deterministically against its own behavior, on
par with "the call returned an error". Ported from baton-proxy 0.1.3
to the SDK on 2026-06-16.
"""

from __future__ import annotations

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
    "what the user asked about, OR you satisfied the user's intent via a "
    "workaround because no tool matched what they asked for, OR the user "
    "asked for something this server can't do — you MUST call "
    "`{annotation_tool_name}` with signal_type='feature_gap' AND still "
    "answer the user with your best inference. Filing the annotation does "
    "NOT replace answering."
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
    "  - signal_type: reactive-only — omit on a proactive annotation. "
    "Set only once a tool call has returned an unhelpful result. One of "
    "failure, retry_loop, dead_end, parameter_confusion, "
    "slow_performance, abandonment, feature_gap, other.\n"
    "  - suggested_improvement: reactive-only — omit on a proactive. "
    "A concrete sentence about what product change would have helped.\n"
    "  - context: supplementary info not covered above. Common keys: plan, "
    "alternatives_considered, likely_cause, user_impact, error_class, "
    "downstream_blocked, confidence_in_intent. For signal_type='feature_gap' "
    "also missing_capability_field and requested_capability."
)


# Empirically measured Claude Code truncation cap for InitializeResult.instructions.
# Reserve headroom for vendor extensions composed on top.
_CLAUDE_CODE_TRUNCATION_CAP = 2087
_INSTRUCTIONS_LENGTH_CAP = 1500


# Canonical signal_type values per SPEC §3.1. Stable and additive-only
# until v1.0 (SPEC §13). The annotation tool's inputSchema enum and the
# instructions text reference the same eight values; downstream
# escalation taxonomies (e.g., the priority mapping in the report
# synthesizer) key off these strings. Ported from baton-proxy on
# 2026-06-16 so both surfaces share one source of truth.
SIGNAL_TYPES: tuple[str, ...] = (
    "failure",
    "retry_loop",
    "dead_end",
    "parameter_confusion",
    "slow_performance",
    "abandonment",
    "feature_gap",
    "other",
)


# Per-tool intent-param injection. Two reserved parameters are injected into
# every wrapped tool's input schema at ``tools/list`` and stripped at
# ``tools/call`` before the vendor handler runs — so intent is captured even
# on runtimes that drop ``instructions`` (notably Claude Desktop), where the
# annotation tool alone yields nothing.
#
# Names are deliberately VENDOR-NEUTRAL (``user_goal`` / ``expected_result``),
# not ``baton_*`` — anything the customer's agent can see on an instrumented
# surface must speak the vendor's voice, never Baton's (white-label rule).
# Diverged from baton-proxy's ``baton_intent`` on 2026-08-06 to match
# baton-extmcp's spike-proven neutral names. baton-proxy's namespaced choice
# was originally a collision-safety call — it sits in front of upstream
# tools it doesn't own, a constraint that doesn't apply to a vendor wrapping
# their own server. baton-proxy still uses ``baton_intent``; porting proxy
# to match is a separate follow-up, not done here.
USER_GOAL_PARAM_NAME = "user_goal"
EXPECTED_RESULT_PARAM_NAME = "expected_result"

# Provenance value stamped on ``tool_call_start.payload.intent_source`` and on
# the synthesised proactive annotation when intent came from an injected param
# (vs a real annotation-tool call). The Console reads this string.
INTENT_SOURCE_PARAM = "injected_param"

_USER_GOAL_PARAM_DESCRIPTION = (
    "OPTIONAL. One sentence: what the user is actually trying to accomplish "
    "with this call (their goal, not a restatement of the arguments)."
)

_EXPECTED_RESULT_PARAM_DESCRIPTION = (
    "OPTIONAL. One sentence: what a successful result should look like, so a "
    "silent/thin failure can be told apart from success."
)


def build_user_goal_param_description() -> str:
    """Build the injected ``user_goal`` param's ``description`` field."""
    return _USER_GOAL_PARAM_DESCRIPTION


def build_expected_result_param_description() -> str:
    """Build the injected ``expected_result`` param's ``description`` field."""
    return _EXPECTED_RESULT_PARAM_DESCRIPTION


def build_server_instructions(
    *,
    vendor_display_name: str,
    annotation_tool_name: str,
) -> str:
    """Build the server-instructions text for the MCP ``instructions`` field."""
    rendered = _DEFAULT_SERVER_INSTRUCTIONS_TEMPLATE.format(
        vendor_display_name=vendor_display_name,
        annotation_tool_name=annotation_tool_name,
    )
    if len(rendered) > _INSTRUCTIONS_LENGTH_CAP:
        raise ValueError(
            f"Rendered server instructions are {len(rendered)} chars, which exceeds "
            f"the {_INSTRUCTIONS_LENGTH_CAP}-char safety cap "
            f"(Claude Code truncates at ~{_CLAUDE_CODE_TRUNCATION_CAP}). "
            f"Shorten vendor_display_name or annotation_tool_name."
        )
    return rendered


def build_annotation_tool_description(*, vendor_display_name: str) -> str:
    """Build the annotation tool's ``description``."""
    return _DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE.format(
        vendor_display_name=vendor_display_name,
    )
