"""Shared LLM-facing text — server instructions, annotation tool description, refusal.

Two adapters (``baton.integrations.standalone``, ``baton.integrations.official``)
surface identical text to the calling agent; this module owns the canonical
copy so they cannot drift.

**Split of responsibility (load-bearing for §5.1.2 under Claude Code's
truncation cap):**

- *Server instructions* carry the MUST/REQUIRED behavioral framing —
  the BEFORE/AFTER/IF triggers, the report fields, and the
  "annotation doesn't replace answering" guardrail. Claude Code
  truncates ``InitializeResult.instructions`` at ~2087 chars, so the
  template is kept under ~1500 chars and the fixed recovery and subagent
  sentences fit in the rest. Loaded once at session init, which is the only point that
  can drive the *first* proactive annotation before any tool is called.
- *Annotation tool description* carries the field-level reference (what
  belongs in user_goal / suggested_improvement / context). Tool descriptions
  are loaded on every call to the central annotation tool, so this is the
  right place for the just-in-time dictionary.

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
from dataclasses import dataclass
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
# param carries the same field on every call without an extra turn.
_INSTRUCTIONS_PROACTIVE_CLAUSE = (
    "BEFORE invoking any {vendor_display_name} tool, you MUST call "
    "`{annotation_tool_name}` with user_goal (REQUIRED).\n\n"
)

# Without it an agent can file the report and stop the user's task.
_INSTRUCTIONS_RECOVERY_SENTENCE = (
    " Then keep going: work out why the call failed, fix the input, and try "
    "again. Do not repeat the same failing call. Reporting is not a "
    "substitute for recovering."
)

# Always present, in both modes — this is the product signal. `again` points
# back at the BEFORE call, so it is rendered only where that call is asked for.
_INSTRUCTIONS_REACTIVE_CLAUSES = (
    "AFTER any {vendor_display_name} tool errors, times out, returns an "
    "unhelpful or contradictory result, or the user shows signs of giving "
    "up, you MUST call `{annotation_tool_name}` {again}with what_happened "
    "(REQUIRED, your own words, NOT a category), tool_name (REQUIRED) and "
    "suggested_improvement (REQUIRED whenever you can articulate one)."
    "{recovery}\n\n"
    "IF a {vendor_display_name} tool response lacks a structured field for "
    "what the user asked about, OR you satisfied the user's intent via a "
    "workaround because no tool matched what they asked for, OR the user "
    "asked for something this server can't do — you MUST call "
    "`{annotation_tool_name}` once per user request, not per call, with "
    "what_happened and tool_name (none if no tool) AND still answer the "
    "user with your best inference. Filing the annotation does NOT replace "
    "answering."
)


_ANNOTATION_LEAD_PROACTIVE = (
    "Record structured signal about a {vendor_display_name} tool call — "
    "what the user is trying to do, and how it went. Populate proactively "
    "before the call (user_goal) and reactively after if the result was "
    "unhelpful (what_happened + tool_name + suggested_improvement).\n"
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
    "  - user_goal: {user_goal}\n"
    "  - what_happened: REQUIRED on a report{omit_on_a_note}. "
    "1-2 plain sentences in YOUR OWN WORDS: what you asked for, "
    "what came back, and why it was unusable. Do NOT pick a category or "
    "invent a label — describe it.\n"
    "  - tool_name: REQUIRED on a report. The {vendor_display_name} tool that "
    "went wrong. Write none if no tool exists for the request.\n"
    "  - suggested_improvement: {report_only}"
    "A concrete sentence about what product change would have helped.\n"
    "  - context: supplementary info not covered above. Common keys: plan, "
    "alternatives_considered, likely_cause, user_impact, error_class, "
    "downstream_blocked, confidence_in_intent. When no tool covers the "
    "request also missing_capability_field and requested_capability."
)


# Empirically measured Claude Code truncation cap for InitializeResult.instructions.
# The cap below it leaves room for the fixed recovery and subagent sentences.
_CLAUDE_CODE_TRUNCATION_CAP = 2087
_INSTRUCTIONS_LENGTH_CAP = 1500


# Per-tool intent-param injection. A reserved parameter is injected into
# every wrapped tool's input schema at ``tools/list`` and stripped at
# ``tools/call`` before the vendor handler runs, so intent is captured even
# on runtimes that drop ``instructions`` (notably Claude Desktop), where the
# annotation tool alone yields nothing.
#
# The name is VENDOR-NEUTRAL, not ``baton_*``: anything the customer's agent
# can see on an instrumented surface speaks the vendor's voice (white-label).
USER_GOAL_PARAM_NAME = "user_goal"

# No longer injected, but a client holding a tool list from before may still
# send them: the vendor's tool would reject the unknown argument, and
# ``overall_task`` carries the turn number the Console falls back to. Removal
# is pinned by ``test_retired_param_recording_is_removed_on_schedule``.
EXPECTED_RESULT_PARAM_NAME = "expected_result"
OVERALL_TASK_PARAM_NAME = "overall_task"
RETIRED_PARAM_NAMES = (EXPECTED_RESULT_PARAM_NAME, OVERALL_TASK_PARAM_NAME)


def pop_goal_params(
    tool_name: str, arguments: dict[str, Any], dispositions: dict[str, str] | None
) -> tuple[str | None, str | None, str | None]:
    """Pop ``user_goal`` and the retired names from ``arguments`` in place and
    return their values; a name the tool declares (``"native"``) is the
    vendor's and stays.

    With no dispositions (a call before the tool was listed) ``user_goal`` is
    stripped with a warning, which is safe because it is reserved. The
    retired names are no longer reserved, so they stay.
    """
    if dispositions is None:
        if USER_GOAL_PARAM_NAME in arguments:
            logger.warning(
                "baton: stripping %s from unlisted tool %r (cold registry)",
                USER_GOAL_PARAM_NAME,
                tool_name,
            )
        return _pop_text(arguments, USER_GOAL_PARAM_NAME), None, None
    goal, expected, task = (
        None if dispositions.get(name) == "native" else _pop_text(arguments, name)
        for name in (USER_GOAL_PARAM_NAME, *RETIRED_PARAM_NAMES)
    )
    return goal, expected, task


def drop_retired_params(arguments: dict[str, Any]) -> None:
    """For a tool whose own schema declares neither retired name."""
    for name in RETIRED_PARAM_NAMES:
        arguments.pop(name, None)


def _pop_text(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.pop(name, None)
    return value if isinstance(value, str) and value.strip() else None


# ``tool_call_start.payload.intent_source``: where the call's intent came from.
# The Console reads these strings.
INTENT_SOURCE_PARAM = "injected_param"
INTENT_SOURCE_LIBRARY = "library_arg"

# The leading number says which user message the call answers, and the
# Console cuts a turn where it changes (SPEC §11.5.1 rule 2). The Console
# splits the rest on "; success is".
#
# Do not reword without scoring the result on live agents. Only the leading
# "OPTIONAL." / "REQUIRED." label tracks ``intent_param_mode``, so it agrees
# with the schema's ``required`` list.
_USER_GOAL_PARAM_BODY = (
    "Write it as '<n>: <what this call is for>; success is <what its result "
    "should show>'. n is the number of the user's current message in this "
    "conversation (1 for the first). Use the same number on every call you make "
    "for that message, including calls made after reading tool results; it goes "
    "up only when the user sends another message."
)
_USER_GOAL_PARAM_DESCRIPTION = "OPTIONAL. " + _USER_GOAL_PARAM_BODY
_USER_GOAL_PARAM_DESCRIPTION_REQUIRED = "REQUIRED. " + _USER_GOAL_PARAM_BODY

# A main agent that delegates may never load this server's tool schemas, so the
# param description above never reaches it and its subagents get no number.
_INSTRUCTIONS_SUBAGENT_CLAUSE = (
    "\n\nWhen you hand work to a subagent that may call this server's tools, tell it "
    "the number of the user's current message in this conversation, and that it "
    "must start user_goal with that number on every call to this server's "
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


def inject_goal_params(schema: Any, *, intent_param_mode: str) -> dict[str, str]:
    """Add ``user_goal`` to a tool's input ``schema`` in place, and to its
    ``required`` under ``"required"`` mode. Return each name's disposition:
    ``"injected"``, or ``"native"`` where the tool declares the name itself
    and keeps it, so its value is forwarded rather than stripped."""
    if not isinstance(schema, dict):
        return {}
    props = schema.setdefault("properties", {})
    if not isinstance(props, dict):
        return {}
    dispositions = {name: "native" for name in RETIRED_PARAM_NAMES if name in props}
    if USER_GOAL_PARAM_NAME in props:
        dispositions[USER_GOAL_PARAM_NAME] = "native"
        return dispositions
    dispositions[USER_GOAL_PARAM_NAME] = "injected"
    props[USER_GOAL_PARAM_NAME] = {
        "type": "string",
        "description": build_user_goal_param_description(intent_param_mode=intent_param_mode),
    }
    if USER_GOAL_PARAM_NAME in required_param_names(intent_param_mode=intent_param_mode):
        required = schema.get("required")
        if not isinstance(required, list):
            schema["required"] = required = []
        if USER_GOAL_PARAM_NAME not in required:
            required.append(USER_GOAL_PARAM_NAME)
    return dispositions


def required_param_names(*, intent_param_mode: str) -> tuple[str, ...]:
    """Which injected params ``intent_param_mode`` advertises as required.

    It answers per MODE, not per tool: a tool that declares the name itself
    keeps its own schema and is not escalated. ``required`` is advertised and
    never enforced (``TestRequiredByDefaultIsNeverEnforced``, both adapters).
    """
    if intent_param_mode != "required":
        return ()
    return (USER_GOAL_PARAM_NAME,)


@dataclass(frozen=True)
class IntentParam:
    """One injected param, for a vendor adding it to its own API (SPEC §5.6).

    ``description`` is the text an agent reads on an MCP server, without the
    leading "OPTIONAL." / "REQUIRED." label: whether the vendor's API requires
    it is the vendor's call. ``trace_kwarg`` is the ``client.trace()`` argument
    the agent's answer goes to.
    """

    name: str
    description: str
    trace_kwarg: str


INTENT_PARAMS: tuple[IntentParam, ...] = (
    IntentParam(USER_GOAL_PARAM_NAME, _USER_GOAL_PARAM_BODY, "intent"),
)


def drop_subagent_clause(mcp: Any, tool_name: str, write: Callable[[Any, str], None]) -> None:
    """Remove the subagent sentence from a server whose tool ``tool_name``
    declares its own ``user_goal``: there the sentence would send the turn
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
        "baton: tool %r declares its own user_goal, so the subagent sentence was "
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
    request, and the word "again" that points back at it.
    ``intent_param_mode="off"`` drops the subagent sentence: no tool then
    carries a ``user_goal`` param for a subagent to fill.
    """
    if proactive_mode == "on":
        template = _INSTRUCTIONS_HEAD_PROACTIVE + _INSTRUCTIONS_PROACTIVE_CLAUSE
    else:
        template = _INSTRUCTIONS_HEAD_REACTIVE_ONLY

    def render(recovery: str) -> str:
        return (template + _INSTRUCTIONS_REACTIVE_CLAUSES).format(
            vendor_display_name=vendor_display_name,
            annotation_tool_name=annotation_tool_name,
            again="again " if proactive_mode == "on" else "",
            recovery=recovery,
        )

    # The cap is the budget the two names share. The fixed sentences have no
    # names in them, so they come out of the headroom under the truncation cap.
    budgeted = len(render(""))
    if budgeted > _INSTRUCTIONS_LENGTH_CAP:
        raise ValueError(
            f"Rendered server instructions are {budgeted} chars, which exceeds "
            f"the {_INSTRUCTIONS_LENGTH_CAP}-char safety cap "
            f"(Claude Code truncates at ~{_CLAUDE_CODE_TRUNCATION_CAP}). "
            f"Shorten vendor_display_name or annotation_tool_name."
        )
    rendered = render(_INSTRUCTIONS_RECOVERY_SENTENCE)
    if intent_param_mode != "off":
        rendered += _INSTRUCTIONS_SUBAGENT_CLAUSE
    return rendered


_VENDOR_INSTRUCTIONS_ATTR = "_baton_vendor_instructions"
# Where our paragraph begins; both heads share it.
_OURS_START = _INSTRUCTIONS_HEAD_PROACTIVE.split("{", 1)[0]


def _ends_in_ours(current: Any, vendor: Any) -> bool:
    """Whether ``current`` is the vendor's text followed by a paragraph of ours."""
    if not isinstance(current, str):
        return False
    prefix = f"{vendor}\n\n" if isinstance(vendor, str) and vendor.strip() else ""
    return current.startswith(prefix + _OURS_START)


def with_vendor_instructions(mcp: Any, ours: str) -> str:
    """SPEC §5.1.2: the vendor's own instructions first, ours below them.

    The runtime cuts at ``_CLAUDE_CODE_TRUNCATION_CAP`` from the end, so the
    order decides whose text is lost. The vendor's text is remembered on the
    server, so a second install replaces our paragraph rather than appending
    another; text the vendor set in between replaces what was remembered.
    """
    current = mcp.instructions
    vendor = getattr(mcp, _VENDOR_INSTRUCTIONS_ATTR, current)
    if not _ends_in_ours(current, vendor):
        vendor = current
        setattr(mcp, _VENDOR_INSTRUCTIONS_ATTR, vendor)
    if not isinstance(vendor, str) or not vendor.strip():
        return ours
    combined = f"{vendor}\n\n{ours}"
    if len(combined) > _CLAUDE_CODE_TRUNCATION_CAP:
        logger.warning(
            "baton: the server instructions are %d chars (yours %d, Baton's %d); "
            "Claude Code cuts them at ~%d, so the end of Baton's text is lost there",
            len(combined),
            len(vendor),
            len(ours),
            _CLAUDE_CODE_TRUNCATION_CAP,
        )
    return combined


def build_annotation_tool_description(
    *, vendor_display_name: str, proactive_mode: str = "off"
) -> str:
    """Build the annotation tool's ``description``.

    ``proactive_mode="off"`` (the default) reframes the tool as reactive-only:
    the same fields, but the agent is told to call it after a bad result
    rather than before every call. It refuses a note, so no field's text
    describes one.
    """
    takes_notes = proactive_mode == "on"
    lead = (_ANNOTATION_LEAD_PROACTIVE if takes_notes else _ANNOTATION_LEAD_REACTIVE_ONLY).format(
        vendor_display_name=vendor_display_name
    )
    return _DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE.format(
        vendor_display_name=vendor_display_name,
        lead=lead,
        user_goal=_USER_GOAL_PARAM_BODY,
        omit_on_a_note=" — omit on a proactive annotation" if takes_notes else "",
        report_only="reactive-only — omit on a proactive. " if takes_notes else "",
    )


def build_refusal_text(*, annotation_tool_name: str) -> str:
    """What a reports-only tool answers when a call has no account (SPEC §5.1.1)."""
    return (
        f"{annotation_tool_name} is reactive-only on this server. Call it only "
        "AFTER a tool call returns an unhelpful, empty, failed or contradictory "
        "result, or when no tool covers what the user asked for. What the user is "
        "trying to do is already recorded on each tool call, so no pre-call "
        "annotation is needed — nothing was lost. If a tool call really went "
        "wrong, or no tool covers the request, call this again and say "
        "what_happened. If neither is true, do NOT re-send: that would file a "
        "report for a call that did not go wrong."
    )
