"""Shared LLM-facing text — server instructions, annotation tool description, refusal.

Two adapters (``baton.integrations.standalone``, ``baton.integrations.official``)
surface identical text to the calling agent; this module owns the canonical
copy so they cannot drift.

The instructions are one sentence pointing at the annotation tool, and the
tool's description holds the triggers, the recovery rule and the fields, so
each rule is written once. Claude Code truncates ``InitializeResult.instructions``
at ~2087 chars, so the rendered pointer is capped at 1500.

Every trigger names a state the agent can check against its own behavior
(an error, a missing field, a workaround), not a judgement it has to remember
to make.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baton._overlays import INSTRUCTION_PIECES, Overlay, changed_text, overlaid
from baton._white_space import is_blank
from baton.scrub import scrub_or_none

if TYPE_CHECKING:
    from baton.integrations._annotation_name import AnnotationNames

logger = logging.getLogger(__name__)

# Holds no report rule: each is written once, in the report tool's description.
_INSTRUCTIONS_REPORT_POINTER = (
    "Use `{annotation_tool_name}` to report a {vendor_display_name} tool call "
    "that went wrong, or a request no tool covers; its description says when and how."
)

# The format SPEC §5.1.1 gives for user_goal.
_USER_GOAL_FORMAT = "'<n>: <what this call is for>; success is <what its result should show>'"

# Keep the key list: without it, agents leave context empty.
_CONTEXT_FIELD = (
    "  - context: supplementary info not covered above. Common keys: plan, "
    "alternatives_considered, likely_cause, user_impact, error_class, "
    "downstream_blocked, confidence_in_intent. When no tool covers the "
    "request also missing_capability_field and requested_capability."
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
    "  - user_goal: the same " + _USER_GOAL_FORMAT + " as on this server's other tools.\n"
    "  - what_happened: REQUIRED. 1-2 plain sentences in your own words: what you "
    "asked for, what came back, and why it was unusable. Not a category.\n"
    "  - tool_name: REQUIRED. The {vendor_display_name} tool that went wrong, or "
    "none if no tool exists for the request.\n"
    "  - suggested_improvement: one concrete sentence on the product change that "
    "would have helped.\n" + _CONTEXT_FIELD
)


# Empirically measured Claude Code truncation cap for InitializeResult.instructions.
# The cap below it leaves room for the fixed subagent sentence.
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


Goals = tuple[str | None, str | None, str | None]
"""``user_goal`` and the two retired names, in that order."""


def scrub_goals(scrubber: Callable[[Any], Any], goals: Goals, log: logging.Logger) -> Goals:
    """Each goal through the vendor's scrubber; one that raises becomes ``None``
    rather than failing the call (SPEC §11.2)."""
    return (
        scrub_or_none(scrubber, goals[0], USER_GOAL_PARAM_NAME, log),
        scrub_or_none(scrubber, goals[1], EXPECTED_RESULT_PARAM_NAME, log),
        scrub_or_none(scrubber, goals[2], OVERALL_TASK_PARAM_NAME, log),
    )


def intent_source(goals: Goals) -> str | None:
    """``tool_call_start.payload.intent_source`` for these goals."""
    return INTENT_SOURCE_PARAM if any(g is not None for g in goals) else None


def drop_retired_params(arguments: dict[str, Any]) -> None:
    """For a tool whose own schema declares neither retired name."""
    for name in RETIRED_PARAM_NAMES:
        arguments.pop(name, None)


def _pop_text(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.pop(name, None)
    return value if isinstance(value, str) and not is_blank(value) else None


# ``tool_call_start.payload.intent_source``: where the call's intent came from.
# The Console reads these strings.
INTENT_SOURCE_PARAM = "injected_param"
INTENT_SOURCE_LIBRARY = "library_arg"

# The leading number says which user message the call answers (SPEC §5.1.1).
#
# Do not reword without scoring the result on live agents. Only the leading
# "OPTIONAL." / "REQUIRED." label tracks ``intent_param_mode``, so it agrees
# with the schema's ``required`` list.
_USER_GOAL_PARAM_BODY = (
    f"Write it as {_USER_GOAL_FORMAT}. n is the number of the user's current message in this "
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
        setattr(mcp, _DECLARES_USER_GOAL_ATTR, True)
        if not remove_subagent_clause(mcp, write):
            return
    except Exception:
        logger.exception("baton: could not remove the subagent sentence from the instructions")
        return
    logger.warning(
        "baton: tool %r declares its own user_goal, so the subagent sentence was "
        "removed from the server instructions; a session that already started keeps it",
        tool_name,
    )


def remove_subagent_clause(mcp: Any, write: Callable[[Any, str], None]) -> bool:
    """``drop_subagent_clause`` without its warning; whether there was one to remove."""
    ours = getattr(mcp, _OURS_ATTR, None)
    subagent = getattr(mcp, _SUBAGENT_ATTR, None)
    vendor = getattr(mcp, _VENDOR_INSTRUCTIONS_ATTR, None)
    if (
        not isinstance(ours, str)
        or not subagent
        or mcp.instructions != join_instructions(vendor, ours)
    ):
        return False
    kept = ours.removesuffix(subagent).removesuffix("\n\n")
    write_ours(mcp, kept, write, subagent=None)
    return True


_DECLARES_USER_GOAL_ATTR = "_baton_a_tool_declares_user_goal"


def a_tool_declares_user_goal(mcp: Any) -> bool:
    return getattr(mcp, _DECLARES_USER_GOAL_ATTR, False) is True


def declares_user_goal(schema: Any) -> bool:
    """Whether a tool's input ``schema`` declares ``user_goal`` itself."""
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    return isinstance(props, Mapping) and USER_GOAL_PARAM_NAME in props


_VENDOR_SERVED_PIECES = (*INSTRUCTION_PIECES, "report_tool.description", "report_tool.refusal")


def served_agent_text(
    *,
    names: AnnotationNames | None,
    intent_param_mode: str,
    surface_tools: list[Any],
    vendor_serves_text: bool = False,
    server_overlay: Overlay | None = None,
) -> dict[str, dict[str, str] | None]:
    """``seam_augmentations.agent_text`` (SPEC §11.4.2): Baton's own text as
    served to the vendor-true ``surface_tools``, after ``server_overlay``. A
    piece not served is absent. With ``vendor_serves_text`` the instructions
    and report tool are the vendor's to serve, so their pieces are null."""
    schemas = [
        tool.get("inputSchema") if isinstance(tool, Mapping) else None for tool in surface_tools
    ]
    native = [declares_user_goal(schema) for schema in schemas]
    served: dict[str, str] = {}
    if not any(native):
        served["instructions.subagent"] = INTENT_INSTRUCTIONS
    if any(
        isinstance(schema, Mapping) and not n for schema, n in zip(schemas, native, strict=True)
    ):
        served["goal_param.description"] = build_user_goal_param_description(
            intent_param_mode=intent_param_mode
        )
    if names is not None:
        served["instructions.report_pointer"] = build_report_pointer(
            vendor_display_name=names.vendor_display_name,
            annotation_tool_name=names.annotation_tool_name,
        )
        served["report_tool.description"] = build_annotation_tool_description(
            vendor_display_name=names.vendor_display_name
        )
        served["report_tool.refusal"] = build_refusal_text(
            annotation_tool_name=names.annotation_tool_name
        )
    pieces: dict[str, dict[str, str] | None] = {}
    for piece, text in served.items():
        source = "default"
        if server_overlay is not None and piece in INSTRUCTION_PIECES:
            changed, source = changed_text(text, server_overlay.piece_actions(piece))
            text = text if changed is None else changed
        pieces[piece] = {"text": text, "source": source}
    if vendor_serves_text:
        pieces.update(dict.fromkeys(_VENDOR_SERVED_PIECES))
    return pieces


def instructions_suffix(agent_text: Mapping[str, Any]) -> bool | None:
    """Whether Baton's text is in the served instructions; ``None`` when unknown."""
    pieces = [agent_text[piece] for piece in INSTRUCTION_PIECES if piece in agent_text]
    return None if None in pieces else bool(pieces)


def build_report_pointer(*, vendor_display_name: str, annotation_tool_name: str) -> str:
    return _INSTRUCTIONS_REPORT_POINTER.format(
        vendor_display_name=vendor_display_name, annotation_tool_name=annotation_tool_name
    )


def build_server_instructions(
    *,
    vendor_display_name: str,
    annotation_tool_name: str,
) -> str:
    """Build the server-instructions text for the MCP ``instructions`` field."""
    rendered = build_report_pointer(
        vendor_display_name=vendor_display_name, annotation_tool_name=annotation_tool_name
    )
    if len(rendered) > _INSTRUCTIONS_LENGTH_CAP:
        raise ValueError(
            f"Rendered server instructions are {len(rendered)} chars, which exceeds "
            f"the {_INSTRUCTIONS_LENGTH_CAP}-char safety cap "
            f"(Claude Code truncates at ~{_CLAUDE_CODE_TRUNCATION_CAP}). "
            f"Shorten vendor_display_name or annotation_tool_name."
        )
    return rendered + _INSTRUCTIONS_SUBAGENT_CLAUSE


_VENDOR_INSTRUCTIONS_ATTR = "_baton_vendor_instructions"
_OURS_ATTR = "_baton_instructions"
_SUBAGENT_ATTR = "_baton_subagent_sentence"


def join_instructions(vendor: Any, ours: str) -> str:
    if not isinstance(vendor, str) or is_blank(vendor):
        return ours
    return f"{vendor}\n\n{ours}" if ours else vendor


def write_ours(
    mcp: Any,
    ours: str,
    write: Callable[[Any, str], None],
    warned: set[str] | None = None,
    *,
    subagent: str | None = INTENT_INSTRUCTIONS,
) -> None:
    """Write ``ours`` below the vendor's instructions (SPEC §5.1.2).
    ``subagent`` is the subagent sentence ``ours`` ends with, if it does.

    What was written is remembered on the server once the write succeeds, so a
    second install or a new setting replaces our paragraph rather than
    appending another; text the vendor set in between replaces what was
    remembered.
    """
    vendor = _vendor_text(mcp)
    write(mcp, with_vendor_instructions(mcp, ours, warned))
    setattr(mcp, _VENDOR_INSTRUCTIONS_ATTR, vendor)
    setattr(mcp, _OURS_ATTR, ours)
    setattr(mcp, _SUBAGENT_ATTR, subagent if subagent and ours.endswith(subagent) else None)


def _vendor_text(mcp: Any) -> Any:
    current = mcp.instructions
    written = getattr(mcp, _OURS_ATTR, None)
    vendor = getattr(mcp, _VENDOR_INSTRUCTIONS_ATTR, current)
    return (
        vendor
        if isinstance(written, str) and current == join_instructions(vendor, written)
        else current
    )


def with_vendor_instructions(mcp: Any, ours: str, warned: set[str] | None = None) -> str:
    """The instructions ``write_ours`` writes: the vendor's first, ours below.
    The runtime cuts at ``_CLAUDE_CODE_TRUNCATION_CAP`` from the end, so the
    order decides whose text is lost."""
    vendor = _vendor_text(mcp)
    combined = join_instructions(vendor, ours)
    if combined == ours:
        return ours
    # ``warned`` is shared by servers built per request, so this is said once.
    if len(combined) > _CLAUDE_CODE_TRUNCATION_CAP and _first_time(warned, "instructions over cap"):
        logger.warning(
            "baton: the server instructions are %d chars (yours %d, Baton's %d); "
            "Claude Code cuts them at ~%d, so the end of Baton's text is lost there",
            len(combined),
            len(vendor),
            len(ours),
            _CLAUDE_CODE_TRUNCATION_CAP,
        )
    return combined


def _first_time(warned: set[str] | None, key: str) -> bool:
    if warned is None:
        return True
    if key in warned:
        return False
    warned.add(key)
    return True


def build_annotation_tool_description(*, vendor_display_name: str) -> str:
    """Build the annotation tool's ``description``: the report rules the
    instructions only point at."""
    return _REPORT_TOOL_DESCRIPTION_TEMPLATE.format(vendor_display_name=vendor_display_name)


def build_refusal_text(*, annotation_tool_name: str, overlay: Overlay | None = None) -> str:
    """What the annotation tool answers when a call has no account (SPEC §5.1.1),
    after the caller's ``overlay``."""
    text = (
        f"{annotation_tool_name} is reactive-only on this server. Call it only "
        "AFTER a tool call returns an unhelpful, empty, failed or contradictory "
        "result, or when no tool covers what the user asked for. What the user is "
        "trying to do is already recorded on each tool call, so no pre-call "
        "annotation is needed — nothing was lost. If a tool call really went "
        "wrong, or no tool covers the request, call this again and say "
        "what_happened. If neither is true, do NOT re-send: that would file a "
        "report for a call that did not go wrong."
    )
    return overlaid(text, overlay, "report_tool.refusal")
