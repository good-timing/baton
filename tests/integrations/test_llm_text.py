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
    milder framing under-populates fields. Don't drop it accidentally."""
    rendered = build_server_instructions(
        vendor_display_name="Acme",
        annotation_tool_name="acme_annotate",
    )
    assert "BEFORE" in rendered
    assert "AFTER" in rendered
    assert "MUST" in rendered
    assert "REQUIRED" in rendered


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
        "intent",
        "expected_outcome",
        "workflow",
        "signal_type",
        "suggested_improvement",
        "context",
    ):
        assert field in rendered, f"annotation field {field!r} missing from description"
