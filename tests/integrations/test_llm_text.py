"""Regression tests for the shared LLM-facing text templates.

Two load-bearing properties — both tied to Claude Code's empirically observed
~2087-char truncation cap on ``InitializeResult.instructions``:

1. The base server-instructions template MUST stay well under the cap so
   vendor extensions composed on top have budget to work with.

2. Both adapters (``baton.integrations.fastmcp`` and ``baton.integrations.mcp``)
   MUST surface IDENTICAL text — they share the canonical copy in
   ``baton.integrations._llm_text``.
"""

from __future__ import annotations

from baton.integrations._llm_text import (
    build_annotation_tool_description,
    build_server_instructions,
)
from baton.integrations.fastmcp.instructions import (
    build_server_instructions as fastmcp_build,
)
from baton.integrations.mcp.instructions import (
    build_server_instructions as mcp_build,
)

# Empirically measured cap is ~2087 chars; we leave a generous margin for
# vendor extensions composed on top and for the worst-case vendor_display_name
# length expansion.
_INSTRUCTIONS_LENGTH_CAP = 1500


def test_instructions_under_truncation_cap() -> None:
    rendered = build_server_instructions(
        vendor_display_name="VeryLongVendorDisplayName Inc.",
        annotation_tool_name="very_long_vendor_display_name_annotate",
    )
    assert len(rendered) <= _INSTRUCTIONS_LENGTH_CAP, (
        f"Server-instructions template grew to {len(rendered)} chars — "
        f"Claude Code truncates at ~2087. Keep under {_INSTRUCTIONS_LENGTH_CAP} "
        f"to leave headroom for vendor BatonExtensions."
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


def test_adapter_instructions_identical() -> None:
    """Both adapters re-export from the shared module — they must produce
    identical text for identical inputs."""
    kwargs = {"vendor_display_name": "Acme", "annotation_tool_name": "acme_annotate"}
    assert fastmcp_build(**kwargs) == mcp_build(**kwargs) == build_server_instructions(**kwargs)


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
