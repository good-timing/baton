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
  belongs in user_goal / expected_result / overall_task / suggested_improvement /
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

# The head differs by proactive_mode only in what it promises the tool is for:
# with proactive on it records intent AND outcomes, with it off the injected
# params carry intent and the tool is the friction channel alone.
_INSTRUCTIONS_HEAD_PROACTIVE = (
    "This server is wrapped in the {vendor_display_name} support-signal SDK. "
    "Use `{annotation_tool_name}` to record what the user is trying to do and "
    "how each {vendor_display_name} tool call went. See that tool's "
    "description for field-level detail.\n\n"
)

_INSTRUCTIONS_HEAD_REACTIVE_ONLY = (
    "This server is wrapped in the {vendor_display_name} support-signal SDK. "
    "Use `{annotation_tool_name}` to report when a {vendor_display_name} tool "
    "call goes wrong. See that tool's description for field-level detail.\n\n"
)

# Requested only when proactive_mode == "on". Off by default: the injected
# params carry the same three fields on every call without an extra turn.
_INSTRUCTIONS_PROACTIVE_CLAUSE = (
    "BEFORE invoking any {vendor_display_name} tool, you MUST call "
    "`{annotation_tool_name}` with user_goal (REQUIRED), expected_result "
    "(REQUIRED), and overall_task (REQUIRED): a short stable label for "
    "the broader task this call serves (e.g., 'morning meeting prep'), "
    "repeated verbatim until the user starts a different task.\n\n"
)

# Always present, in both modes — this is the product signal.
_INSTRUCTIONS_REACTIVE_CLAUSES = (
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


_ANNOTATION_LEAD_PROACTIVE = (
    "Record structured signal about a {vendor_display_name} tool call — "
    "what the user is trying to do, and how it went. Populate proactively "
    "before the call (user_goal + expected_result + overall_task) and "
    "reactively "
    "after if the result was unhelpful (signal_type + suggested_improvement).\n"
)

# proactive_mode="off": the injected params already carry intent on every
# call, so asking for a pre-call annotation here would reintroduce exactly the
# extra turn the mode exists to remove. intent stays REQUIRED because a
# reactive annotation still needs to say what was being attempted.
_ANNOTATION_LEAD_REACTIVE_ONLY = (
    "Report a {vendor_display_name} tool call that went wrong — call this "
    "AFTER a call returns an unhelpful, empty, failed or contradictory "
    "result, or when no tool covers what the user asked for. Do NOT call it "
    "before a tool call or to narrate normal successful work.\n"
)

_DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE = (
    "{lead}"
    "\n"
    "Fields:\n"
    "  - user_goal: one sentence on what the user is trying to "
    "accomplish.\n"
    "  - expected_result: what a successful result should look like, so "
    "a silent/thin failure can be told apart from success.\n"
    "  - overall_task: short stable label for the broader task this call "
    "serves, e.g., 'morning meeting prep', 'pre-outreach research'. "
    "REPEAT the exact same string on every call serving the same task; "
    "change it only when the user starts a different task.\n"
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
# The task-label grouping key (wire field ``call_workflow``; console rung 3b).
# Deliberately NOT named ``workflow``: injected params live inside vendor tool
# schemas, where ``workflow`` is a plausible real vendor param (Workfront
# approvals, CI pipelines, Notion automations) — a collision would make the
# strip swallow the vendor's own argument, and the name would invite the LLM
# to fill in the vendor object it is touching instead of the meta task label.
OVERALL_TASK_PARAM_NAME = "overall_task"

# Provenance value stamped on ``tool_call_start.payload.intent_source`` and on
# the synthesised proactive annotation when intent came from an injected param
# (vs a real annotation-tool call). The Console reads this string.
INTENT_SOURCE_PARAM = "injected_param"

# The leading label has to track ``intent_param_mode``: under ``"required"``
# the injector appends ``user_goal`` to the schema's advertised ``required``
# list, so a description still opening "OPTIONAL." contradicts the schema it
# ships inside — the model reads both. Only the label moves; the body is
# byte-identical across modes, because that sentence is the measured text and
# the mode is not a licence to reword it. ``expected_result`` and
# ``overall_task`` are never added to ``required`` in any mode, so their
# "OPTIONAL." is true everywhere and they get no variant.
_USER_GOAL_PARAM_BODY = (
    "One sentence: what the user is actually trying to accomplish "
    "with this call (their goal, not a restatement of the arguments)."
)
_USER_GOAL_PARAM_DESCRIPTION = "OPTIONAL. " + _USER_GOAL_PARAM_BODY
_USER_GOAL_PARAM_DESCRIPTION_REQUIRED = "REQUIRED. " + _USER_GOAL_PARAM_BODY

_EXPECTED_RESULT_PARAM_DESCRIPTION = (
    "OPTIONAL. One sentence: what a successful result should look like, so a "
    "silent/thin failure can be told apart from success."
)

# The stability contract is the load-bearing design element: user_goal/
# expected_result are call-scoped diagnostics that reword freely, so they
# cannot key grouping; this param works ONLY if the model repeats the label
# verbatim while the task is unchanged (measured 2026-08-10: without the
# contract, 80% of adjacent same-task calls reword their goal text).
#
# Granularity is a KNOWN, MEASURED weakness of this text, kept anyway because
# the obvious fix is worse. Do not reword without scoring against both corpora
# in baton-internal `spikes/overall_task_a5/` (40 paired live-agent sessions,
# 2026-08-11, one build per run).
#
# What this text gets wrong: when the user switches topic WITHOUT announcing it,
# agents carry the first task's label onto everything after it — one session
# labelled a rice lookup, a chickpea restock and a waste check all
# "cook dal tonight". Boundary detection 0.700 on cue-free multi-task scripts
# (1.000 when the user says "Different thing:", which is why an earlier run
# missed this entirely).
#
# What it gets right, and why it stays: it never splits a task that should stay
# whole — 20/20 same-task pairs held the label verbatim across both corpora.
# The candidate rewording ("the specific task the user is working on right now
# — not the overall theme of the conversation") fixes the boundary problem
# completely (1.000) but relabels *within* a single task, describing successive
# steps of one goal as different tasks; it scored 0.200 then 0.400 over-split on
# identical scripts, and produced an A → B → A label that a merge-only,
# adjacency-based consumer resolves as three tasks instead of one. The gain
# (+0.300 boundary) is smaller than the cost (0.400 over-split), and shattering
# is the failure mode that destroys downstream trust, so the trade goes this way.
#
# The open target for any v3 is therefore specific: the candidate's boundary
# behaviour with this text's within-task stability. The two failure modes are
# independent, so it is not a granularity dial to be tuned — it needs the
# repeat-verbatim contract hardened against step-level rewording.
_OVERALL_TASK_PARAM_DESCRIPTION = (
    "OPTIONAL. Short stable label for the broader task this call serves "
    "(e.g. 'prepare campaign approval'). REPEAT the exact same string on "
    "every call serving the same task; change it only when the user starts "
    "a different task."
)


def build_user_goal_param_description(*, intent_param_mode: str = "optional") -> str:
    """Build the injected ``user_goal`` param's ``description`` field.

    ``intent_param_mode="required"`` swaps the leading label for "REQUIRED.",
    matching the ``required`` entry the injector adds under that mode. Any
    other mode (including the ``"optional"`` default) returns the text
    unchanged.
    """
    if intent_param_mode == "required":
        return _USER_GOAL_PARAM_DESCRIPTION_REQUIRED
    return _USER_GOAL_PARAM_DESCRIPTION


def build_expected_result_param_description() -> str:
    """Build the injected ``expected_result`` param's ``description`` field."""
    return _EXPECTED_RESULT_PARAM_DESCRIPTION


def build_overall_task_param_description() -> str:
    """Build the injected ``overall_task`` param's ``description`` field."""
    return _OVERALL_TASK_PARAM_DESCRIPTION


def build_server_instructions(
    *,
    vendor_display_name: str,
    annotation_tool_name: str,
    proactive_mode: str = "off",
) -> str:
    """Build the server-instructions text for the MCP ``instructions`` field.

    ``proactive_mode="off"`` (the default) drops the pre-call annotation
    request; the reactive clauses are identical in both modes.
    """
    if proactive_mode == "on":
        template = _INSTRUCTIONS_HEAD_PROACTIVE + _INSTRUCTIONS_PROACTIVE_CLAUSE
    else:
        template = _INSTRUCTIONS_HEAD_REACTIVE_ONLY
    rendered = (template + _INSTRUCTIONS_REACTIVE_CLAUSES).format(
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


def build_annotation_tool_description(
    *, vendor_display_name: str, proactive_mode: str = "off"
) -> str:
    """Build the annotation tool's ``description``.

    ``proactive_mode="off"`` (the default) reframes the tool as reactive-only:
    same fields, but the agent is told to call it after a bad result rather
    than before every call.
    """
    lead = (
        _ANNOTATION_LEAD_PROACTIVE if proactive_mode == "on" else _ANNOTATION_LEAD_REACTIVE_ONLY
    ).format(vendor_display_name=vendor_display_name)
    return _DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE.format(
        vendor_display_name=vendor_display_name,
        lead=lead,
    )
