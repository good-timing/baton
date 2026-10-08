"""Shared LLM-facing text — server instructions + annotation tool description.

Two adapters (``baton.integrations.standalone``, ``baton.integrations.official``)
surface identical text to the calling agent; this module owns the canonical
copy so they cannot drift.

**Split of responsibility (load-bearing for §5.1.2 under Claude Code's
truncation cap):**

- *Server instructions* carry the MUST/REQUIRED behavioral framing —
  the BEFORE/AFTER/IF triggers, the signal_type enum, and the
  "annotation doesn't replace answering" guardrail. Claude Code
  truncates ``InitializeResult.instructions`` at ~2087 chars, so the
  template is kept under ~1500 chars and the fixed subagent sentence
  added after it fits in the rest. Loaded once at session init, which is the only point that
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

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# The head differs by proactive_mode in what it promises the tool is for:
# with proactive on it records intent AND outcomes, with it off the injected
# params carry intent and the tool is the friction channel alone.
#
# Only the reactive-only head names the missing-tool case and why filing
# matters. The proactive head is shorter on purpose, not by oversight: with
# that wording the 30-char long-name fixture rendered 1610 chars in proactive
# mode, and 1557 with just the "so ... can improve their product" reason,
# against the 1500 cap. The IF block already makes the missing-tool case
# mandatory in both modes, so here the head would only repeat it.
_INSTRUCTIONS_HEAD_PROACTIVE = (
    "This server is wrapped in the {vendor_display_name} usage and friction SDK. "
    "Use `{annotation_tool_name}` to record what the user is trying to do and "
    "how each {vendor_display_name} tool call went. See that tool's "
    "description for field-level detail.\n\n"
)

_INSTRUCTIONS_HEAD_REACTIVE_ONLY = (
    "This server is wrapped in the {vendor_display_name} usage and friction SDK. "
    "Use `{annotation_tool_name}` to report when a {vendor_display_name} tool "
    "call goes wrong, or when a tool you needed does not exist, so "
    "{vendor_display_name} can improve their product. See that tool's "
    "description for field-level detail.\n\n"
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
# The cap below it leaves room for ``_INSTRUCTIONS_SUBAGENT_CLAUSE``.
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


# Per-tool intent-param injection. Three reserved parameters are injected into
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
# their own server. baton-proxy ported to these names on 2026-08-08, closing
# the SPEC §13 divergence; the two producers now advertise the same three.
USER_GOAL_PARAM_NAME = "user_goal"
EXPECTED_RESULT_PARAM_NAME = "expected_result"
# The task label (wire field ``call_workflow``).
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
# the injector appends the escalated params to the schema's advertised
# ``required`` list, so a description still opening "OPTIONAL." contradicts the
# schema it ships inside — the model reads both. Only the label moves; the body
# is byte-identical across modes, because that sentence is the measured text and
# the mode is not a licence to reword it.
_USER_GOAL_PARAM_BODY = (
    "One sentence: what the user is actually trying to accomplish "
    "with this call (their goal, not a restatement of the arguments)."
)
_USER_GOAL_PARAM_DESCRIPTION = "OPTIONAL. " + _USER_GOAL_PARAM_BODY
_USER_GOAL_PARAM_DESCRIPTION_REQUIRED = "REQUIRED. " + _USER_GOAL_PARAM_BODY

_EXPECTED_RESULT_PARAM_BODY = (
    "One sentence: what a successful result should look like, so a "
    "silent/thin failure can be told apart from success."
)
_EXPECTED_RESULT_PARAM_DESCRIPTION = "OPTIONAL. " + _EXPECTED_RESULT_PARAM_BODY
_EXPECTED_RESULT_PARAM_DESCRIPTION_REQUIRED = "REQUIRED. " + _EXPECTED_RESULT_PARAM_BODY

# Two parts with two jobs. The leading number says which user message the
# call answers, and the Console cuts a turn where it changes (SPEC §11.5.1
# rule 2). The label after the colon says which task, and it works ONLY if the
# model repeats it verbatim while the task is unchanged: user_goal and
# expected_result reword freely, so they cannot key grouping.
#
# Do not reword without scoring the result on live agents.
_OVERALL_TASK_PARAM_BODY = (
    "The number of the user's current message in this conversation "
    "(1 for the first), a colon, then a short stable label for the broader "
    "task this call serves (e.g. '3: prepare campaign approval'). Use the same "
    "number on every call you make for that message, including calls made "
    "after reading tool results; it goes up only when the user sends another "
    "message. REPEAT the exact same label text after the colon on every call "
    "serving the same task, across messages; change the label only when the "
    "user starts a different task."
)
_OVERALL_TASK_PARAM_DESCRIPTION = "OPTIONAL. " + _OVERALL_TASK_PARAM_BODY
_OVERALL_TASK_PARAM_DESCRIPTION_REQUIRED = "REQUIRED. " + _OVERALL_TASK_PARAM_BODY

# A main agent that delegates may never load this server's tool schemas, so the
# param description above never reaches it and its subagents get no number.
_INSTRUCTIONS_SUBAGENT_CLAUSE = (
    "\n\nWhen you hand work to a subagent that may call this server's tools, tell it "
    "the number of the user's current message in this conversation, and that it "
    "must start overall_task with that number on every call to this server's "
    "tools. This applies only to this server's tools."
)


def build_user_goal_param_description(*, intent_param_mode: str = "optional") -> str:
    """Build the injected ``user_goal`` param's ``description`` field.

    ``intent_param_mode="required"`` swaps the leading label for "REQUIRED.",
    matching the ``required`` entry the injector adds under that mode. Any
    other mode returns the "OPTIONAL." text. This parameter's own default is
    that base text, not the product default: ``VendorConfig.intent_param_mode``
    defaults to ``"required"``, and both adapters always pass their mode.
    """
    if intent_param_mode == "required":
        return _USER_GOAL_PARAM_DESCRIPTION_REQUIRED
    return _USER_GOAL_PARAM_DESCRIPTION


def build_expected_result_param_description(*, intent_param_mode: str = "optional") -> str:
    """Build the injected ``expected_result`` param's ``description`` field.

    Mirrors ``build_user_goal_param_description``: under ``"required"`` the
    leading label becomes "REQUIRED.", matching the ``required`` entry the
    injector adds under that mode. The body is byte-identical
    across modes. This parameter's own default is the "OPTIONAL." text, not the
    product default — ``VendorConfig.intent_param_mode`` defaults to
    ``"required"``, and both adapters always pass their mode.
    """
    if intent_param_mode == "required":
        return _EXPECTED_RESULT_PARAM_DESCRIPTION_REQUIRED
    return _EXPECTED_RESULT_PARAM_DESCRIPTION


def required_param_names(*, intent_param_mode: str) -> tuple[str, ...]:
    """Which injected params ``intent_param_mode`` advertises as required.

    Read by both adapters' injectors and both ``build_seam_augmentations``
    call sites. It answers per MODE, not per tool: a tool that declares one of
    these names itself keeps its own schema and is not escalated.

    ``required`` is advertised and never enforced: a call omitting the param is
    served as it would be unwrapped, pinned for all three names by
    ``TestRequiredByDefaultIsNeverEnforced`` on both adapters.
    """
    if intent_param_mode != "required":
        return ()
    return (USER_GOAL_PARAM_NAME, EXPECTED_RESULT_PARAM_NAME, OVERALL_TASK_PARAM_NAME)


def build_overall_task_param_description(*, intent_param_mode: str = "optional") -> str:
    """Build the injected ``overall_task`` param's ``description`` field.

    Mirrors ``build_user_goal_param_description``: only the leading label
    tracks the mode.
    """
    if intent_param_mode == "required":
        return _OVERALL_TASK_PARAM_DESCRIPTION_REQUIRED
    return _OVERALL_TASK_PARAM_DESCRIPTION


def drop_subagent_clause(mcp: Any, tool_name: str, write: Callable[[Any, str], None]) -> None:
    """Remove the subagent sentence from a server whose tool ``tool_name``
    declares its own ``overall_task``: there the sentence would send the turn
    number into the vendor's argument."""
    # Never raises: it runs inside tool registration and tool listing, which
    # must not fail over this.
    try:
        current = mcp.instructions
        if not isinstance(current, str) or _INSTRUCTIONS_SUBAGENT_CLAUSE not in current:
            return
        write(mcp, current.replace(_INSTRUCTIONS_SUBAGENT_CLAUSE, ""))
    except Exception:
        logger.exception("baton: could not remove the subagent sentence from the instructions")
        return
    logger.warning(
        "baton: tool %r declares its own overall_task, so the subagent sentence was "
        "removed from the server instructions; a session that already started keeps it",
        tool_name,
    )


def build_server_instructions(
    *,
    vendor_display_name: str,
    annotation_tool_name: str,
    proactive_mode: str = "off",
    intent_param_mode: str = "required",
) -> str:
    """Build the server-instructions text for the MCP ``instructions`` field.

    ``proactive_mode="off"`` (the default) drops the pre-call annotation
    request; the reactive clauses are identical in both modes.
    ``intent_param_mode="off"`` drops the subagent sentence: no tool then
    carries an ``overall_task`` param for a subagent to fill.
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
    # Added after the cap check: the sentence has a fixed length, so it comes
    # out of the headroom under the truncation point and not out of the budget
    # the two names share.
    if intent_param_mode != "off":
        rendered += _INSTRUCTIONS_SUBAGENT_CLAUSE
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
