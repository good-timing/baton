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
    _INSTRUCTIONS_LENGTH_CAP,
    build_annotation_tool_description,
    build_server_instructions,
)


def test_instructions_under_truncation_cap() -> None:
    rendered = build_server_instructions(
        vendor_display_name="VeryLongVendorDisplayName Inc.",
        annotation_tool_name="very_long_vendor_display_name_annotate",
    )
    assert len(rendered) <= _INSTRUCTIONS_LENGTH_CAP


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


def test_instructions_carry_full_signal_type_enum() -> None:
    """All 8 signal_type enum values must remain in the rendered text — vendor
    patches downstream key escalation behavior off this enum."""
    rendered = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
    )
    for value in (
        "failure",
        "retry_loop",
        "dead_end",
        "parameter_confusion",
        "slow_performance",
        "abandonment",
        "feature_gap",
        "other",
    ):
        assert value in rendered, f"signal_type value {value!r} missing"


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
        "signal_type",
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


def test_annotation_description_marks_signal_type_reactive_only() -> None:
    """``signal_type`` and ``suggested_improvement`` are reactive-only
    fields — populating them on a proactive annotation makes the
    annotation read as a friction signal it isn't. Proxy 0.1.3 made
    this explicit in the description; SDK must match so the same
    discipline lands in SDK-instrumented vendors' agent transcripts."""
    rendered = build_annotation_tool_description(vendor_display_name="Acme")
    assert "signal_type: reactive-only" in rendered
    assert "suggested_improvement: reactive-only" in rendered


def test_signal_types_constant_matches_spec() -> None:
    """Canonical SPEC §3.1 enum tuple — annotation tool schemas key off
    this constant so they can't drift from the rendered prose."""
    from baton.integrations._llm_text import SIGNAL_TYPES

    assert SIGNAL_TYPES == (
        "failure",
        "retry_loop",
        "dead_end",
        "parameter_confusion",
        "slow_performance",
        "abandonment",
        "feature_gap",
        "other",
    )


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
        assert "signal_type" in off
        assert "suggested_improvement" in off
        assert "feature_gap" in off
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
        assert len(on) <= _INSTRUCTIONS_LENGTH_CAP

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
                "signal_type",
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
