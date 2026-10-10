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

import pytest

from baton.integrations._llm_text import (
    _CLAUDE_CODE_TRUNCATION_CAP,
    build_annotation_tool_description,
    build_overall_task_param_description,
    build_server_instructions,
    required_param_names,
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
    """The agent describes and the worker groups (SPEC §11.5.5): neither text
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
        "expected_result",
        "overall_task",
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


def test_annotation_description_marks_the_report_fields_reactive_only() -> None:
    """Filling a report field on a proactive annotation makes it read as a
    report it is not, so the description says which fields are report-only."""
    rendered = build_annotation_tool_description(vendor_display_name="Acme")
    assert "what_happened: REQUIRED on a report — omit on a proactive" in rendered
    assert "suggested_improvement: reactive-only" in rendered


class TestProactiveMode:
    """``VendorConfig.proactive_mode`` gates ONLY the pre-call annotation
    request. The reactive channel is the product signal and must survive
    unchanged in both modes — a regression here silently deletes the friction
    capture the whole SDK exists to produce.
    """

    def test_off_is_the_default(self) -> None:
        assert build_server_instructions(
            vendor_display_name="Acme", annotation_tool_name="acme_annotate"
        ) == build_server_instructions(
            vendor_display_name="Acme",
            annotation_tool_name="acme_annotate",
            proactive_mode="off",
        )

    def test_off_drops_only_the_pre_call_request(self) -> None:
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

    def test_reactive_text_is_byte_identical_across_modes(self) -> None:
        kw = {"vendor_display_name": "Acme", "annotation_tool_name": "acme_annotate"}
        on = build_server_instructions(**kw, proactive_mode="on")
        off = build_server_instructions(**kw, proactive_mode="off")
        marker = "AFTER any Acme tool errors"
        assert on[on.index(marker) :] == off[off.index(marker) :]

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
                "expected_result",
                "overall_task",
                "what_happened",
                "tool_name",
                "suggested_improvement",
            ):
                assert field in desc, (mode, field)

    def test_tool_description_off_steers_away_from_pre_call_use(self) -> None:
        off = build_annotation_tool_description(vendor_display_name="Acme", proactive_mode="off")
        assert "Do NOT call it before a tool call" in off
        assert "Populate proactively" not in off


# The retired agent-facing names. These are still the WIRE keys, so they
# legitimately appear all over the codebase — but never in text an agent reads,
# where they would name a param that no longer exists.
RETIRED_AGENT_FACING_NAMES = ("intent", "expected_outcome", "workflow")


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
    ``overall_task``; the text is the one the rig runs scored."""

    BODY = (
        "The number of the user's current message in this conversation "
        "(1 for the first), a colon, then a short stable label for the broader "
        "task this call serves (e.g. '3: prepare campaign approval'). Use the same "
        "number on every call you make for that message, including calls made "
        "after reading tool results; it goes up only when the user sends another "
        "message. REPEAT the exact same label text after the colon on every call "
        "serving the same task, across messages; change the label only when the "
        "user starts a different task."
    )
    SUBAGENTS = (
        "When you hand work to a subagent that may call this server's tools, tell it "
        "the number of the user's current message in this conversation, and that it "
        "must start overall_task with that number on every call to this server's "
        "tools. This applies only to this server's tools."
    )

    def test_the_param_description_is_the_scored_text(self) -> None:
        assert build_overall_task_param_description(intent_param_mode="required") == (
            "REQUIRED. " + self.BODY
        )
        assert build_overall_task_param_description(intent_param_mode="optional") == (
            "OPTIONAL. " + self.BODY
        )

    def test_required_mode_escalates_all_three_params(self) -> None:
        assert required_param_names(intent_param_mode="required") == (
            "user_goal",
            "expected_result",
            "overall_task",
        )
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
