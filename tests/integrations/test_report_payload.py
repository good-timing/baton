"""The report fields on the MCP path: scrubbed, and ``tool_name`` keeps its
three states (SPEC §5.1.1)."""

from __future__ import annotations

from typing import Any

import pytest

from baton.integrations._annotation_payload import build_annotation_payload


def _redact(value: Any) -> Any:
    return value.replace("jane@example.com", "[REDACTED]") if isinstance(value, str) else value


def _build(**report: Any) -> Any:
    fields: dict[str, Any] = {
        "user_goal": "look something up",
        "expected_result": None,
        "overall_task": None,
        "suggested_improvement": None,
        "context": None,
        "what_happened": None,
        "tool_name": None,
    }
    return build_annotation_payload(_redact, **{**fields, **report})


def test_the_report_text_and_the_tool_name_go_through_the_scrubber() -> None:
    payload = _build(
        what_happened="asked for jane@example.com; got a 500",
        tool_name="lookup jane@example.com",
    )
    assert payload.what_happened == "asked for [REDACTED]; got a 500"
    assert payload.tool_name == "lookup [REDACTED]"


@pytest.mark.parametrize(("sent", "on_the_wire"), [("search", "search"), ("", ""), (None, None)])
def test_no_tool_stays_apart_from_not_stated(sent: str | None, on_the_wire: str | None) -> None:
    assert _build(what_happened="it failed", tool_name=sent).tool_name == on_the_wire


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_account_is_not_a_report(blank: str | None) -> None:
    assert _build(what_happened=blank).what_happened is None


def test_this_sdk_never_sends_signal_type() -> None:
    assert _build(what_happened="it failed").signal_type is None
