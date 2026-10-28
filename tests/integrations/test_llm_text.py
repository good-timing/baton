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
from packaging.version import Version

import baton
from baton.integrations._llm_text import (
    _CLAUDE_CODE_TRUNCATION_CAP,
    _OURS_STARTS,
    build_annotation_tool_description,
    build_refusal_text,
    build_server_instructions,
    build_user_goal_param_description,
    pop_goal_params,
    required_param_names,
    with_vendor_instructions,
)


@pytest.mark.parametrize("proactive_mode", ["off", "on"])
def test_instructions_under_truncation_cap(proactive_mode: str) -> None:
    """A 30-char display name fits in both modes. This rendered only the
    default until 2026-09-11, so a proactive head at 1610 chars passed CI."""
    rendered = build_server_instructions(
        vendor_display_name="VeryLongVendorDisplayName Inc.",
        annotation_tool_name="very_long_vendor_display_name_annotate",
        proactive_mode=proactive_mode,
    )
    assert len(rendered) < _CLAUDE_CODE_TRUNCATION_CAP


def test_instructions_raises_when_names_exceed_cap() -> None:
    """build_server_instructions raises ValueError if rendered length exceeds cap,
    rather than silently returning a string Claude Code will truncate."""
    with pytest.raises(ValueError, match="exceeds the"):
        build_server_instructions(
            vendor_display_name="A" * 200,
            annotation_tool_name="a_annotate",
        )


@pytest.mark.parametrize("proactive_mode", ["off", "on"])
def test_the_error_paragraph_tells_the_agent_to_carry_on(proactive_mode: str) -> None:
    """Without the sentence an agent can file the report and stop the user's task."""
    rendered = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
        proactive_mode=proactive_mode,
    )
    error_paragraph = next(p for p in rendered.split("\n\n") if p.startswith("AFTER"))
    assert error_paragraph.endswith(
        "suggested_improvement (REQUIRED whenever you can articulate one). "
        "Then keep going: work out why the call failed, fix the input, and try "
        "again. Do not repeat the same failing call. Reporting is not a "
        "substitute for recovering."
    )


@pytest.mark.parametrize("proactive_mode", ["off", "on"])
def test_the_recovery_sentence_leaves_the_name_budget_alone(proactive_mode: str) -> None:
    """The longest names that fit are the same with and without the sentence."""
    longest = {"off": 68, "on": 56}[proactive_mode]
    build_server_instructions(
        vendor_display_name="X" * longest,
        annotation_tool_name="x" * longest,
        proactive_mode=proactive_mode,
    )
    with pytest.raises(ValueError, match="exceeds the"):
        build_server_instructions(
            vendor_display_name="X" * (longest + 1),
            annotation_tool_name="x" * (longest + 1),
            proactive_mode=proactive_mode,
        )


def test_instructions_carry_must_required_framing() -> None:
    """The BEFORE/AFTER MUST/REQUIRED framing is load-bearing per SPEC §5.4 —
    milder framing under-populates fields. Don't drop it accidentally.

    BEFORE is proactive-only; the reactive framing must survive in both modes.
    """
    rendered = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
        proactive_mode="on",
    )
    assert "BEFORE" in rendered
    assert "AFTER" in rendered
    assert "MUST" in rendered
    assert "REQUIRED" in rendered

    default = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
    )
    assert "BEFORE" not in default, "proactive_mode defaults to off"
    assert "AFTER" in default
    assert "MUST" in default
    assert "REQUIRED" in default


@pytest.mark.parametrize("proactive_mode", ["off", "on"])
def test_no_agent_facing_text_offers_a_category_to_pick(proactive_mode: str) -> None:
    """The agent describes and does not classify (SPEC §5.1.1): neither text
    names ``signal_type`` or any of its eight values as something to fill in."""
    instructions = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
        proactive_mode=proactive_mode,
    )
    description = build_annotation_tool_description(
        vendor_display_name="Acme", proactive_mode=proactive_mode
    )
    for text in (instructions, description):
        for word in ("signal_type", "retry_loop", "dead_end", "parameter_confusion", "feature_gap"):
            assert word not in text, word
    assert "what_happened (REQUIRED, your own words, NOT a category)" in instructions
    assert "tool_name (REQUIRED)" in instructions


def test_a_missing_tool_is_reported_once_per_request() -> None:
    rendered = build_server_instructions(
        vendor_display_name="Acme", annotation_tool_name="acme_annotate"
    )
    assert "once per user request, not per call" in rendered


def test_instructions_carry_dont_replace_answering_guardrail() -> None:
    """Without this guardrail the agent treats annotation as proxy-satisfaction
    and stops answering the user — documented failure mode."""
    rendered = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
    )
    assert "does NOT replace answering" in rendered


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


def test_instructions_carry_three_mechanical_triggers() -> None:
    """Per the 2026-06-12 live-Claude finding (ported from baton-proxy
    0.1.3), every signal_type prompt needs a mechanical trigger — an
    observable state Claude can check at the end of a tool call —
    rather than a vigilance trigger. The IF block must surface all
    three triggers: (1) lacks structured field, (2) intent satisfied
    via workaround because no tool matched, (3) user asked for
    something this server can't do."""
    rendered = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
    )
    assert "lacks a structured field" in rendered
    assert "workaround because no tool matched" in rendered
    assert "asked for something this server can't do" in rendered


def test_missing_tool_case_is_mandatory_in_both_modes_and_headlined_when_off() -> None:
    """The IF block is where the missing-tool case is mandatory, so it is pinned
    there in both modes. The reactive-only head, the default and the first thing
    the agent reads, names it too. The proactive head does not: that clause
    pushed the long-name fixture past the cap, and in that mode it only
    repeated the IF block."""
    for mode in ("on", "off"):
        rendered = build_server_instructions(
            vendor_display_name="Acme", annotation_tool_name="acme_annotate", proactive_mode=mode
        )
        if_block = rendered[rendered.index("IF a Acme tool") :]
        assert "workaround because no tool matched" in if_block, mode
        assert "asked for something this server can't do" in if_block, mode

    off = build_server_instructions(
        vendor_display_name="Acme", annotation_tool_name="acme_annotate", proactive_mode="off"
    )
    head = off[: off.index("\n\n")]
    assert "a tool you needed does not exist" in head
    assert "so Acme can improve their product" in head


def test_a_tool_that_takes_notes_marks_the_report_fields_report_only() -> None:
    """Filling a report field on a proactive annotation makes it read as a
    report it is not, so the description says which fields are report-only."""
    rendered = build_annotation_tool_description(vendor_display_name="Acme", proactive_mode="on")
    assert "what_happened: REQUIRED on a report — omit on a proactive annotation. 1-2" in rendered
    assert "suggested_improvement: reactive-only — omit on a proactive. A concrete" in rendered


def test_a_reports_only_tool_does_not_describe_a_note() -> None:
    """It refuses a note, and a field that says how to send one invites it."""
    rendered = build_annotation_tool_description(vendor_display_name="Acme")
    assert "proactive" not in rendered
    assert "what_happened: REQUIRED on a report. 1-2 plain sentences" in rendered
    assert "suggested_improvement: A concrete sentence" in rendered


class TestProactiveMode:
    """``VendorConfig.proactive_mode`` gates the pre-call annotation request
    and the one word that points back at it. The reactive channel is the product signal and must survive in
    both modes — a regression here silently deletes the friction capture the
    whole SDK exists to produce.
    """

    def test_off_is_the_default(self) -> None:
        assert build_server_instructions(
            vendor_display_name="Acme", annotation_tool_name="acme_annotate"
        ) == build_server_instructions(
            vendor_display_name="Acme",
            annotation_tool_name="acme_annotate",
            proactive_mode="off",
        )

    def test_off_drops_the_pre_call_request(self) -> None:
        off = build_server_instructions(
            vendor_display_name="Acme",
            annotation_tool_name="acme_annotate",
            proactive_mode="off",
        )
        assert "BEFORE invoking" not in off
        assert "expected_outcome" not in off
        # Reactive clauses verbatim, both of them.
        assert "AFTER any Acme tool errors" in off
        assert "what_happened" in off
        assert "suggested_improvement" in off
        assert "no tool matched" in off
        assert "NOT replace answering" in off

    def test_reactive_text_differs_across_modes_by_one_back_reference(self) -> None:
        """The word "again" points at the BEFORE call, which one mode asks for."""
        kw = {"vendor_display_name": "Acme", "annotation_tool_name": "acme_annotate"}
        on = build_server_instructions(**kw, proactive_mode="on")
        off = build_server_instructions(**kw, proactive_mode="off")
        marker = "AFTER any Acme tool errors"
        on_tail, off_tail = on[on.index(marker) :], off[off.index(marker) :]
        assert "`acme_annotate` again with" in on_tail
        assert on_tail.replace("`acme_annotate` again with", "`acme_annotate` with") == off_tail

    def test_off_is_shorter_and_still_under_cap(self) -> None:
        kw = {"vendor_display_name": "Acme", "annotation_tool_name": "acme_annotate"}
        on = build_server_instructions(**kw, proactive_mode="on")
        off = build_server_instructions(**kw, proactive_mode="off")
        assert len(off) < len(on)
        assert len(on) < _CLAUDE_CODE_TRUNCATION_CAP

    def test_tool_description_keeps_every_field_in_both_modes(self) -> None:
        """The tool's FIELD contract is mode-independent — only the lead
        sentence changes. A reactive annotation still carries intent."""
        for mode in ("on", "off"):
            desc = build_annotation_tool_description(
                vendor_display_name="Acme", proactive_mode=mode
            )
            for field in (
                "user_goal",
                "what_happened",
                "tool_name",
                "suggested_improvement",
            ):
                assert field in desc, (mode, field)

    def test_tool_description_off_steers_away_from_pre_call_use(self) -> None:
        off = build_annotation_tool_description(vendor_display_name="Acme", proactive_mode="off")
        assert "Do NOT call it before a tool call" in off
        assert "Populate proactively" not in off


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

    Both description tests check that each CURRENT field name appears. That
    passed while `_ANNOTATION_LEAD_PROACTIVE` still told the agent to populate
    "intent + expected_outcome + workflow" — three names the schema no longer
    accepts — because the field dictionary below it listed the new ones and the
    assertion only ever asked "is the new name here somewhere".

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
        "instructions (proactive on)": build_server_instructions(
            vendor_display_name="Acme", annotation_tool_name="acme_annotate", proactive_mode="on"
        ),
        "instructions (proactive off)": build_server_instructions(
            vendor_display_name="Acme", annotation_tool_name="acme_annotate", proactive_mode="off"
        ),
        "tool description (on)": build_annotation_tool_description(
            vendor_display_name="Acme", proactive_mode="on"
        ),
        "tool description (off)": build_annotation_tool_description(
            vendor_display_name="Acme", proactive_mode="off"
        ),
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

    @pytest.mark.parametrize("proactive_mode", ["off", "on"])
    def test_the_annotation_field_is_the_scored_text(self, proactive_mode: str) -> None:
        desc = build_annotation_tool_description(
            vendor_display_name="Acme", proactive_mode=proactive_mode
        )
        assert "\n  - user_goal: " + self.BODY + "\n" in desc

    def test_required_mode_escalates_the_one_param(self) -> None:
        assert required_param_names(intent_param_mode="required") == ("user_goal",)
        assert required_param_names(intent_param_mode="optional") == ()

    @pytest.mark.parametrize("proactive_mode", ["off", "on"])
    def test_instructions_end_with_the_subagent_sentence(self, proactive_mode: str) -> None:
        """A main agent that never loads the tool schema reads only this."""
        rendered = build_server_instructions(
            vendor_display_name="Acme",
            annotation_tool_name="acme_annotate",
            proactive_mode=proactive_mode,
        )
        assert rendered.endswith("\n\n" + self.SUBAGENTS)

    def test_no_subagent_sentence_when_no_param_is_injected(self) -> None:
        rendered = build_server_instructions(
            vendor_display_name="Acme",
            annotation_tool_name="acme_annotate",
            proactive_mode="on",
            intent_param_mode="off",
        )
        assert "subagent" not in rendered

    def test_the_sentence_spends_headroom_and_not_the_name_budget(self) -> None:
        """The longest text that renders still clears Claude Code's cut."""
        kw = {"annotation_tool_name": "a_annotate", "proactive_mode": "on"}
        longest = max(
            n
            for n in range(1, 400)
            if _renders(vendor_display_name="A" * n, intent_param_mode="off", **kw)
        )
        with_sentence = build_server_instructions(vendor_display_name="A" * longest, **kw)
        without = build_server_instructions(
            vendor_display_name="A" * longest, intent_param_mode="off", **kw
        )
        assert len(with_sentence) == len(without) + len("\n\n" + self.SUBAGENTS)
        assert len(with_sentence) < _CLAUDE_CODE_TRUNCATION_CAP


def _renders(**kwargs: str) -> bool:
    try:
        build_server_instructions(**kwargs)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("proactive_mode", ["off", "on"])
def test_no_tool_is_asked_for_as_the_word_none(proactive_mode: str) -> None:
    """Shown ``""`` or told "empty string", agents send the quote characters."""
    instructions = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
        proactive_mode=proactive_mode,
    )
    description = build_annotation_tool_description(
        vendor_display_name="Acme", proactive_mode=proactive_mode
    )

    assert "tool_name (none if no tool)" in instructions
    assert "Write none if no tool exists" in description
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


def test_both_heads_open_with_the_text_that_marks_our_paragraph() -> None:
    for mode in ("off", "on"):
        rendered = build_server_instructions(
            vendor_display_name="Acme", annotation_tool_name="acme_annotate", proactive_mode=mode
        )
        assert rendered.startswith(_OURS_STARTS[0])


class TestWithVendorInstructions:
    """SPEC §5.1.2 across repeated installs on one server."""

    OURS = "This server is wrapped in the Acme usage and friction SDK. More."

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


# The release after next. Releases bump the patch number, so a stale tool list
# from before the single-field release has been refreshed by then.
_RETIRED_RECORDING_REMOVED_IN = (0, 8, 24)


def test_retired_param_recording_is_removed_on_schedule() -> None:
    """Remove ``pop_retired_params`` and the ``call_expected`` /
    ``call_workflow`` it feeds in both adapters; SPEC §13 has the plan."""
    current = Version(baton.__version__).release
    assert current < _RETIRED_RECORDING_REMOVED_IN, (
        "time to remove the retired expected_result / overall_task recording"
    )
