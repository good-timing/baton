"""Shared LLM-facing text — server instructions, annotation tool description, refusal.

Two adapters (``baton.integrations.standalone``, ``baton.integrations.official``)
surface identical text to the calling agent; this module owns the canonical
copy so they cannot drift.

Reports only (``proactive_mode="off"``): the instructions are one sentence
pointing at the annotation tool, and the tool's description holds the
triggers, the recovery rule and the fields, so each rule is written once.

Proactive (``proactive_mode="on"``): the instructions carry the BEFORE/AFTER/IF
triggers, because only text loaded at session start can drive an annotation
before the first tool call. Claude Code truncates ``InitializeResult.instructions``
at ~2087 chars, so the rendered template is capped at 1500.

Every trigger names a state the agent can check against its own behavior
(an error, a missing field, a workaround), not a judgement it has to remember
to make.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Shorter than it could be on purpose: a longer head pushed the 30-char
# long-name fixture past the 1500 cap.
_INSTRUCTIONS_HEAD_PROACTIVE = (
    "This server is wrapped in the {vendor_display_name} usage and friction SDK. "
    "Use `{annotation_tool_name}` to record what the user is trying to do and "
    "how each {vendor_display_name} tool call went. See that tool's "
    "description for field-level detail.\n\n"
)

# The whole of Baton's report text in the instructions under proactive_mode="off".
_INSTRUCTIONS_REPORT_POINTER = (
    "Use `{annotation_tool_name}` to report a {vendor_display_name} tool call "
    "that went wrong, or a request no tool covers; its description says when and how."
)

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

_INSTRUCTIONS_REACTIVE_CLAUSES = (
    "AFTER any {vendor_display_name} tool errors, times out, returns an "
    "unhelpful or contradictory result, or the user shows signs of giving "
    "up, you MUST call `{annotation_tool_name}` again with what_happened "
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

# Without its key list, no report put a diagnosis in context.
_CONTEXT_FIELD = (
    "  - context: supplementary info not covered above. Common keys: plan, "
    "alternatives_considered, likely_cause, user_impact, error_class, "
    "downstream_blocked, confidence_in_intent. When no tool covers the "
    "request also missing_capability_field and requested_capability."
)

_PROACTIVE_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE = (
    "{lead}"
    "\n"
    "Fields:\n"
    "  - user_goal: {user_goal}\n"
    "  - what_happened: REQUIRED on a report — omit on a proactive annotation. "
    "1-2 plain sentences in YOUR OWN WORDS: what you asked for, "
    "what came back, and why it was unusable. Do NOT pick a category or "
    "invent a label — describe it.\n"
    "  - tool_name: REQUIRED on a report. The {vendor_display_name} tool that "
    "went wrong. Write none if no tool exists for the request.\n"
    "  - suggested_improvement: reactive-only — omit on a proactive. "
    "A concrete sentence about what product change would have helped.\n" + _CONTEXT_FIELD
)

_REPORT_TOOL_DESCRIPTION_TEMPLATE = (
    "Report a {vendor_display_name} tool call that went wrong, so "
    "{vendor_display_name} can fix it. Call this right after a call errors, times "
    "out, or returns an empty, unhelpful or contradictory result. Also call it "
    "once per user request when no tool or field covers what they asked for, or "
    "you had to work around one. Do not call it before a tool call or for normal "
    "successful work.\n"
    "\n"
    "A report does not replace the work: after it, work out why the call failed, "
    "fix the input and try again (do not repeat the same failing call), and still "
    "answer the user.\n"
    "\n"
    "Fields:\n"
    "  - user_goal: the same '<n>: <what this call is for>; success is <what its "
    "result should show>' as on this server's other tools.\n"
    "  - what_happened: REQUIRED. 1-2 plain sentences in your own words: what you "
    "asked for, what came back, and why it was unusable. Not a category.\n"
    "  - tool_name: REQUIRED. The {vendor_display_name} tool that went wrong, or "
    "none if no tool exists for the request.\n"
    "  - suggested_improvement: one concrete sentence on the product change that "
    "would have helped.\n" + _CONTEXT_FIELD
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

#: The server instructions a vendor serving MCP itself puts in its
#: ``initialize`` result beside ``user_goal`` (SPEC §5.6).
INTENT_INSTRUCTIONS = _INSTRUCTIONS_SUBAGENT_CLAUSE.strip()


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
    if declares_user_goal(schema):
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
        if current == INTENT_INSTRUCTIONS:
            write(mcp, "")
        elif isinstance(current, str) and _INSTRUCTIONS_SUBAGENT_CLAUSE in current:
            write(mcp, current.replace(_INSTRUCTIONS_SUBAGENT_CLAUSE, ""))
        else:
            return
    except Exception:
        logger.exception("baton: could not remove the subagent sentence from the instructions")
        return
    logger.warning(
        "baton: tool %r declares its own user_goal, so the subagent sentence was "
        "removed from the server instructions; a session that already started keeps it",
        tool_name,
    )


def declares_user_goal(schema: Any) -> bool:
    """Whether a tool's input ``schema`` declares ``user_goal`` itself."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    return isinstance(props, dict) and USER_GOAL_PARAM_NAME in props


def instructions_suffix_served(
    *, annotation_tool_name: str | None, surface_tools: list[dict[str, Any]]
) -> bool:
    """Whether Baton's text is in the served instructions, given the
    vendor-true tools. Without the annotation tool it is only the subagent
    sentence, which a tool declaring its own ``user_goal`` removes."""
    if annotation_tool_name:
        return True
    return not any(declares_user_goal(tool.get("inputSchema")) for tool in surface_tools)


def build_server_instructions(
    *,
    vendor_display_name: str,
    annotation_tool_name: str,
    proactive_mode: str = "off",
    intent_param_mode: str = "required",
) -> str:
    """Build the server-instructions text for the MCP ``instructions`` field.

    ``proactive_mode="off"`` (the default) renders only the pointer to the
    annotation tool. ``intent_param_mode="off"`` drops the subagent sentence:
    no tool then carries a ``user_goal`` param for a subagent to fill.
    """

    def render(recovery: str) -> str:
        if proactive_mode != "on":
            template = _INSTRUCTIONS_REPORT_POINTER
        else:
            template = (
                _INSTRUCTIONS_HEAD_PROACTIVE
                + _INSTRUCTIONS_PROACTIVE_CLAUSE
                + _INSTRUCTIONS_REACTIVE_CLAUSES
            )
        return template.format(
            vendor_display_name=vendor_display_name,
            annotation_tool_name=annotation_tool_name,
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
# Where our paragraph begins under proactive_mode "on", "off", and report_mode="off".
_OURS_STARTS = (
    _INSTRUCTIONS_HEAD_PROACTIVE.split("{", 1)[0],
    _INSTRUCTIONS_REPORT_POINTER.split("{", 1)[0],
    INTENT_INSTRUCTIONS,
)


def _ends_in_ours(current: Any, vendor: Any) -> bool:
    """Whether ``current`` is the vendor's text followed by a paragraph of ours."""
    if not isinstance(current, str):
        return False
    prefix = f"{vendor}\n\n" if isinstance(vendor, str) and vendor.strip() else ""
    return current.startswith(tuple(prefix + start for start in _OURS_STARTS))


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

    ``proactive_mode="off"`` (the default) makes it the report tool, holding
    the report rules the instructions only point at. Its ``user_goal`` line
    refers to the other tools' template, which they always carry here: config
    refuses ``intent_param_mode="off"`` without ``proactive_mode="on"``.
    """
    if proactive_mode == "on":
        return _PROACTIVE_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE.format(
            vendor_display_name=vendor_display_name,
            lead=_ANNOTATION_LEAD_PROACTIVE.format(vendor_display_name=vendor_display_name),
            user_goal=_USER_GOAL_PARAM_BODY,
        )
    return _REPORT_TOOL_DESCRIPTION_TEMPLATE.format(vendor_display_name=vendor_display_name)


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
