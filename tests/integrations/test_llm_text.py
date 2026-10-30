"""Regression tests for the shared LLM-facing text templates.

Load-bearing properties tied to Claude Code's empirically observed
~2087-char truncation cap on ``InitializeResult.instructions``:

- The base server-instructions template MUST stay well under the cap so
  vendor extensions composed on top have budget to work with.
- ``build_server_instructions`` MUST raise if rendered output would exceed cap.
- Both adapters import directly from ``baton.integrations._llm_text`` — no
  separate copies to drift.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from baton.integrations._llm_text import (
    _CLAUDE_CODE_TRUNCATION_CAP,
    _INSTRUCTIONS_LENGTH_CAP,
    _OURS_STARTS,
    build_annotation_tool_description,
    build_refusal_text,
    build_server_instructions,
    build_user_goal_param_description,
    pop_goal_params,
    required_param_names,
    with_vendor_instructions,
)


def test_instructions_under_truncation_cap() -> None:
    rendered = build_server_instructions(
        vendor_display_name="VeryLongVendorDisplayName Inc.",
        annotation_tool_name="very_long_vendor_display_name_annotate",
    )
    assert len(rendered) < _CLAUDE_CODE_TRUNCATION_CAP


def test_instructions_raises_when_names_exceed_cap() -> None:
    """build_server_instructions raises ValueError if rendered length exceeds cap,
    rather than silently returning a string Claude Code will truncate."""
    with pytest.raises(ValueError, match="exceeds the"):
        build_server_instructions(vendor_display_name="A" * 1500, annotation_tool_name="a_annotate")


# The scored reports-only text: rewording it needs a new scored run
# (agent_cost_knobs.md, "Trim the report text").
REPORTS_ONLY_INSTRUCTIONS = (
    "Use `toybox_annotate` to report a Toybox Pantry tool call that went wrong, or a "
    "request no tool covers; its description says when and how.\n\nWhen you hand work "
    "to a subagent that may call this server's tools, tell it the number of the user's "
    "current message in this conversation, and that it must start user_goal with that "
    "number on every call to this server's tools. This applies only to this server's "
    "tools."
)
REPORTS_ONLY_TOOL_DESCRIPTION = (
    "Report a Toybox Pantry tool call that went wrong, so Toybox Pantry can fix it. Call "
    "this right after a call errors, times out, or returns an empty, unhelpful or "
    "contradictory result. Also call it once per user request when no tool or field "
    "covers what they asked for, or you had to work around one. Do not call it before a "
    "tool call or for normal successful work.\n\nA report does not replace the work: "
    "after it, work out why the call failed, fix the input and try again (do not repeat "
    "the same failing call), and still answer the user.\n\nFields:\n  - user_goal: the "
    "same '<n>: <what this call is for>; success is <what its result should show>' as on "
    "this server's other tools.\n  - what_happened: REQUIRED. 1-2 plain sentences in "
    "your own words: what you asked for, what came back, and why it was unusable. Not a "
    "category.\n  - tool_name: REQUIRED. The Toybox Pantry tool that went wrong, or none "
    "if no tool exists for the request.\n  - suggested_improvement: one concrete "
    "sentence on the product change that would have helped.\n  - context: "
    "supplementary info not covered above. Common keys: plan, alternatives_considered, "
    "likely_cause, user_impact, error_class, downstream_blocked, confidence_in_intent. "
    "When no tool covers the request also missing_capability_field and "
    "requested_capability."
)


def test_reports_only_text_is_the_scored_text() -> None:
    kw = {"vendor_display_name": "Toybox Pantry"}
    assert (
        build_server_instructions(**kw, annotation_tool_name="toybox_annotate")
        == REPORTS_ONLY_INSTRUCTIONS
    )
    assert build_annotation_tool_description(**kw) == REPORTS_ONLY_TOOL_DESCRIPTION


def test_reports_only_instructions_hold_no_report_rules() -> None:
    """Each rule is written once, in the tool's description."""
    rendered = build_server_instructions(
        vendor_display_name="Acme", annotation_tool_name="acme_annotate"
    )
    for rule in ("AFTER", "MUST", "keep going", "once per user request", "wrapped in"):
        assert rule not in rendered, rule


def test_no_agent_facing_text_offers_a_category_to_pick() -> None:
    """The agent describes and does not classify (SPEC §5.1.1): neither text
    names ``signal_type`` or any of its eight values as something to fill in."""
    instructions = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
    )
    description = build_annotation_tool_description(vendor_display_name="Acme")
    for text in (instructions, description):
        for word in ("signal_type", "retry_loop", "dead_end", "parameter_confusion", "feature_gap"):
            assert word not in text, word


def test_annotation_description_lists_all_fields() -> None:
    rendered = build_annotation_tool_description(vendor_display_name="Acme")
    for field in (
        "user_goal",
        "what_happened",
        "tool_name",
        "suggested_improvement",
        "context",
    ):
        assert field in rendered, f"annotation field {field!r} missing from description"


def test_a_reports_only_tool_does_not_describe_a_note() -> None:
    """It refuses a note, and a field that says how to send one invites it."""
    rendered = build_annotation_tool_description(vendor_display_name="Acme")
    assert "proactive" not in rendered


# Retired agent-facing names. Some are still WIRE keys, so they appear all over
# the codebase — but never in text an agent reads, where they would name a
# param that no longer exists.
RETIRED_AGENT_FACING_NAMES = (
    "intent",
    "expected_outcome",
    "workflow",
    "expected_result",
    "overall_task",
)


def test_no_agent_facing_text_still_asks_for_a_retired_param_name() -> None:
    """Presence tests are not enough, and this is not hypothetical.

    The description test checks that each CURRENT field name appears, and
    would pass beside a line telling the agent to populate a retired name,
    because it only asks "is the new name here somewhere".

    An agent reading the lead line fills in params that are then silently
    dropped: the call succeeds, the annotation emits, and the goal text is
    simply missing.

    Matched only where a param is REFERENCED — `name:` in the field
    dictionary, `name (REQUIRED`, or inside the `a + b + c` populate list. A
    bare word-boundary search over-detects and would fail on this sentence,
    which is prose and correct: "you satisfied the user's intent via a
    workaround". The narrower pattern is not a weakening; the failure being
    guarded is a param NAMED to the agent, and those three positions are the
    only places these templates name one.
    """
    import re

    surfaces = {
        "instructions": build_server_instructions(
            vendor_display_name="Acme", annotation_tool_name="acme_annotate"
        ),
        "tool description": build_annotation_tool_description(vendor_display_name="Acme"),
    }
    for where, text in surfaces.items():
        for retired in RETIRED_AGENT_FACING_NAMES:
            referenced = (
                rf"\b{retired}(?=:)|\b{retired} \(REQUIRED|(?<=\+ ){retired}\b|\b{retired}(?= \+)"
            )
            assert not re.search(referenced, text), (
                f"{where} still names the retired param {retired!r}; agents will send it "
                f"and the value will be dropped"
            )


class TestTurnNumber:
    """The wording asks for the user's message number at the start of
    ``user_goal``; the text is the one the rig runs scored."""

    BODY = (
        "Write it as '<n>: <what this call is for>; success is <what its result "
        "should show>'. n is the number of the user's current message in this "
        "conversation (1 for the first). Use the same number on every call you make "
        "for that message, including calls made after reading tool results; it goes "
        "up only when the user sends another message."
    )
    SUBAGENTS = (
        "When you hand work to a subagent that may call this server's tools, tell it "
        "the number of the user's current message in this conversation, and that it "
        "must start user_goal with that number on every call to this server's "
        "tools. This applies only to this server's tools."
    )

    def test_the_param_description_is_the_scored_text(self) -> None:
        assert build_user_goal_param_description(intent_param_mode="required") == (
            "REQUIRED. " + self.BODY
        )
        assert build_user_goal_param_description(intent_param_mode="optional") == (
            "OPTIONAL. " + self.BODY
        )

    def test_required_mode_escalates_the_one_param(self) -> None:
        assert required_param_names(intent_param_mode="required") == ("user_goal",)
        assert required_param_names(intent_param_mode="optional") == ()

    def test_instructions_end_with_the_subagent_sentence(self) -> None:
        """A main agent that never loads the tool schema reads only this."""
        rendered = build_server_instructions(
            vendor_display_name="Acme", annotation_tool_name="acme_annotate"
        )
        assert rendered.endswith("\n\n" + self.SUBAGENTS)

    def test_the_sentence_spends_headroom_and_not_the_name_budget(self) -> None:
        """The longest text that renders still clears Claude Code's cut."""
        longest = max(
            n
            for n in range(1, 1500)
            if _renders(vendor_display_name="A" * n, annotation_tool_name="a_annotate")
        )
        rendered = build_server_instructions(
            vendor_display_name="A" * longest, annotation_tool_name="a_annotate"
        )
        assert len(rendered) - len("\n\n" + self.SUBAGENTS) == _INSTRUCTIONS_LENGTH_CAP
        assert len(rendered) < _CLAUDE_CODE_TRUNCATION_CAP


def _renders(**kwargs: str) -> bool:
    try:
        build_server_instructions(**kwargs)
    except ValueError:
        return False
    return True


def test_no_tool_is_asked_for_as_the_word_none() -> None:
    """Shown ``""`` or told "empty string", agents send the quote characters."""
    instructions = build_server_instructions(
        vendor_display_name="Acme", annotation_tool_name="acme_annotate"
    )
    description = build_annotation_tool_description(vendor_display_name="Acme")

    assert "or none if no tool exists" in description
    for text in (instructions, description):
        assert '""' not in text
        assert "mpty string" not in text


def test_the_refusal_names_the_next_step_for_each_sender() -> None:
    # The same text as baton-proxy and baton-ts, word for word.
    assert build_refusal_text(annotation_tool_name="acme_annotate") == (
        "acme_annotate is reactive-only on this server. Call it only AFTER a "
        "tool call returns an unhelpful, empty, failed or contradictory result, "
        "or when no tool covers what the user asked for. What the user is "
        "trying to do is already recorded on each tool call, so no pre-call "
        "annotation is needed — nothing was lost. If a tool call really went "
        "wrong, or no tool covers the request, call this again and say "
        "what_happened. If neither is true, do NOT re-send: that would file a "
        "report for a call that did not go wrong."
    )


def test_the_pointer_opens_with_the_text_that_marks_our_paragraph() -> None:
    rendered = build_server_instructions(
        vendor_display_name="Acme", annotation_tool_name="acme_annotate"
    )
    assert _OURS_STARTS[0].match(rendered)


class TestWithVendorInstructions:
    """SPEC §5.1.2 across repeated installs on one server."""

    OURS = build_server_instructions(
        vendor_display_name="Acme", annotation_tool_name="acme_annotate"
    )

    def _server(self, instructions: str | None) -> Any:
        return types.SimpleNamespace(instructions=instructions)

    def test_a_second_install_replaces_our_paragraph(self) -> None:
        mcp = self._server("Vendor v1.")
        mcp.instructions = with_vendor_instructions(mcp, self.OURS)
        assert with_vendor_instructions(mcp, self.OURS) == f"Vendor v1.\n\n{self.OURS}"

    def test_text_the_vendor_set_between_installs_is_kept(self) -> None:
        mcp = self._server("Vendor v1.")
        mcp.instructions = with_vendor_instructions(mcp, self.OURS)
        mcp.instructions = "Vendor v2."
        assert with_vendor_instructions(mcp, self.OURS) == f"Vendor v2.\n\n{self.OURS}"

    def test_vendor_text_added_after_an_install_with_none_is_kept(self) -> None:
        mcp = self._server(None)
        mcp.instructions = with_vendor_instructions(mcp, self.OURS)
        mcp.instructions = "Vendor v2."
        assert with_vendor_instructions(mcp, self.OURS) == f"Vendor v2.\n\n{self.OURS}"


class TestAVendorParagraphLikeTheReportPointer:
    """Our pointer opens "Use `", as a vendor's own paragraph may."""

    OURS = build_server_instructions(
        vendor_display_name="Acme", annotation_tool_name="acme_annotate"
    )
    VENDOR = "Use `search` before `run`."

    def test_vendor_text_set_after_an_install_with_none_is_kept(self) -> None:
        mcp = types.SimpleNamespace(instructions=None)
        mcp.instructions = with_vendor_instructions(mcp, self.OURS)
        mcp.instructions = self.VENDOR
        assert with_vendor_instructions(mcp, self.OURS) == f"{self.VENDOR}\n\n{self.OURS}"

    def test_a_vendor_paragraph_added_below_its_text_is_kept(self) -> None:
        mcp = types.SimpleNamespace(instructions="Base.")
        mcp.instructions = with_vendor_instructions(mcp, self.OURS)
        mcp.instructions = f"Base.\n\n{self.VENDOR}"
        assert with_vendor_instructions(mcp, self.OURS) == (
            f"Base.\n\n{self.VENDOR}\n\n{self.OURS}"
        )

    def test_our_pointer_is_still_replaced(self) -> None:
        mcp = types.SimpleNamespace(instructions="Base.")
        mcp.instructions = with_vendor_instructions(mcp, self.OURS)
        assert with_vendor_instructions(mcp, self.OURS) == f"Base.\n\n{self.OURS}"


def test_retired_names_are_popped_only_where_the_schema_is_known() -> None:
    """With no dispositions the tool may declare a retired name itself, and
    only ``user_goal`` is still reserved, so the vendor's argument stays."""
    sent = {"user_goal": "1: g", "expected_result": "e", "overall_task": "t", "text": "x"}

    unknown = dict(sent)
    assert pop_goal_params("echo", unknown, None) == ("1: g", None, None)
    assert unknown == {"expected_result": "e", "overall_task": "t", "text": "x"}

    native = dict(sent)
    dispositions = {"user_goal": "injected", "overall_task": "native"}
    assert pop_goal_params("echo", native, dispositions) == ("1: g", "e", None)
    assert native == {"overall_task": "t", "text": "x"}

    blank = {"user_goal": " ", "expected_result": "  ", "overall_task": 3}
    assert pop_goal_params("echo", blank, {}) == (None, None, None)
    assert blank == {}
