"""Tests for Baton event schemas per SPEC §11.4.

Spec-first, failing-test-first: this file is written BEFORE ``src/baton/events.py``.
Each test maps to a specific claim from SPEC §11.4 — event-type enum, envelope
fields, per-type payload shapes, JSON round-trip fidelity, and discriminated-union
parsing for the worker side.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from baton.events import (
    AnnotationEvent,
    AnnotationPayload,
    Event,
    ToolCallEndEvent,
    ToolCallEndPayload,
    ToolCallErrorEvent,
    ToolCallErrorPayload,
    ToolCallStartEvent,
    ToolCallStartPayload,
)


def _envelope() -> dict[str, Any]:
    """Common envelope fields a test event needs. event_id + sdk_version +
    event_type are set by defaults on the concrete model."""
    return {
        "tenant_id": "ten_01H4F",
        "session_id": "sess_01H4F",
        "sequence_number": 1,
        "captured_at": datetime.now(UTC),
        "consent_token": "ct_test_01H4F",
        "agent_runtime": "claude-code",
    }


# =============================================================================
# tool_call_start
# =============================================================================


class TestToolCallStartEvent:
    def test_minimal_valid(self) -> None:
        event = ToolCallStartEvent(
            **_envelope(),
            payload=ToolCallStartPayload(tool_name="chat.completions.create"),
        )
        assert event.event_type == "tool_call_start"
        assert event.payload.tool_name == "chat.completions.create"
        assert event.payload.params == {}

    def test_with_params(self) -> None:
        event = ToolCallStartEvent(
            **_envelope(),
            payload=ToolCallStartPayload(
                tool_name="chat.completions.create",
                params={"model": "llama-3.3-70b", "max_tokens": 200},
            ),
        )
        assert event.payload.params["model"] == "llama-3.3-70b"

    def test_event_id_is_uuidv7(self) -> None:
        event = ToolCallStartEvent(**_envelope(), payload=ToolCallStartPayload(tool_name="t"))
        assert event.event_id.version == 7

    def test_sdk_version_default(self) -> None:
        event = ToolCallStartEvent(**_envelope(), payload=ToolCallStartPayload(tool_name="t"))
        assert event.sdk_version.startswith("0.")


# =============================================================================
# tool_call_end
# =============================================================================


class TestToolCallEndEvent:
    def test_minimal(self) -> None:
        event = ToolCallEndEvent(**_envelope(), payload=ToolCallEndPayload(tool_name="t"))
        assert event.event_type == "tool_call_end"

    def test_with_result_and_duration(self) -> None:
        event = ToolCallEndEvent(
            **_envelope(),
            payload=ToolCallEndPayload(tool_name="t", result={"key": "value"}, duration_ms=120),
        )
        assert event.payload.duration_ms == 120
        assert event.payload.result == {"key": "value"}


# =============================================================================
# tool_call_error
# =============================================================================


class TestToolCallErrorEvent:
    def test_required_fields(self) -> None:
        event = ToolCallErrorEvent(
            **_envelope(),
            payload=ToolCallErrorPayload(
                tool_name="t", error_type="TimeoutError", error_body="timed out"
            ),
        )
        assert event.payload.error_type == "TimeoutError"

    def test_missing_error_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ToolCallErrorPayload.model_validate({"tool_name": "t", "error_body": "..."})

    def test_missing_error_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ToolCallErrorPayload.model_validate({"tool_name": "t", "error_type": "X"})


# =============================================================================
# annotation
# =============================================================================


class TestAnnotationEvent:
    def test_all_fields_nullable(self) -> None:
        """Per SPEC §11.4, every annotation field is nullable — the agent
        populates what it has. Proactive call populates intent/expected;
        reactive populates signal_type/suggested_improvement; etc."""
        event = AnnotationEvent(**_envelope(), payload=AnnotationPayload())
        assert event.payload.intent is None
        assert event.payload.signal_type is None
        assert event.payload.workflow is None
        assert event.payload.suggested_improvement is None
        assert event.payload.context is None

    def test_proactive_annotation_shape(self) -> None:
        event = AnnotationEvent(
            **_envelope(),
            payload=AnnotationPayload(
                intent="summarize PR comments",
                expected_outcome="2-3 sentence paragraph",
                workflow="code-review",
            ),
        )
        assert event.payload.intent == "summarize PR comments"
        assert event.payload.signal_type is None

    def test_reactive_annotation_shape(self) -> None:
        event = AnnotationEvent(
            **_envelope(),
            payload=AnnotationPayload(
                signal_type="dead_end",
                suggested_improvement="surface clearer error",
                context={"likely_cause": "content_filter"},
            ),
        )
        assert event.payload.signal_type == "dead_end"
        assert event.payload.context is not None
        assert event.payload.context["likely_cause"] == "content_filter"


# =============================================================================
# Envelope behavior shared across all event types
# =============================================================================


class TestSequenceNumber:
    def test_non_negative_allowed(self) -> None:
        envelope = _envelope()
        envelope["sequence_number"] = 0
        event = ToolCallStartEvent(**envelope, payload=ToolCallStartPayload(tool_name="t"))
        assert event.sequence_number == 0

    def test_negative_rejected(self) -> None:
        envelope = _envelope()
        envelope["sequence_number"] = -1
        with pytest.raises(ValidationError):
            ToolCallStartEvent(**envelope, payload=ToolCallStartPayload(tool_name="t"))


class TestAgentRuntime:
    def test_default_unknown(self) -> None:
        envelope = _envelope()
        envelope.pop("agent_runtime")
        event = ToolCallStartEvent(**envelope, payload=ToolCallStartPayload(tool_name="t"))
        assert event.agent_runtime == "unknown"

    def test_explicit_runtime(self) -> None:
        event = ToolCallStartEvent(**_envelope(), payload=ToolCallStartPayload(tool_name="t"))
        assert event.agent_runtime == "claude-code"


# =============================================================================
# JSON round-trip — events must serialize/deserialize losslessly
# =============================================================================


class TestJsonRoundTrip:
    def test_tool_call_start(self) -> None:
        original = ToolCallStartEvent(
            **_envelope(),
            payload=ToolCallStartPayload(tool_name="t", params={"k": "v"}),
        )
        rebuilt = ToolCallStartEvent.model_validate_json(original.model_dump_json())
        assert rebuilt.event_id == original.event_id
        assert rebuilt.payload.tool_name == "t"
        assert rebuilt.payload.params == {"k": "v"}
        assert rebuilt.sequence_number == original.sequence_number

    def test_tool_call_end(self) -> None:
        original = ToolCallEndEvent(
            **_envelope(),
            payload=ToolCallEndPayload(tool_name="t", result=[1, 2, 3], duration_ms=99),
        )
        rebuilt = ToolCallEndEvent.model_validate_json(original.model_dump_json())
        assert rebuilt.payload.duration_ms == 99
        assert rebuilt.payload.result == [1, 2, 3]

    def test_annotation_with_nested_context(self) -> None:
        original = AnnotationEvent(
            **_envelope(),
            payload=AnnotationPayload(
                intent="x",
                signal_type="dead_end",
                context={"likely_cause": "X", "nested": {"a": 1, "b": [2, 3]}},
            ),
        )
        rebuilt = AnnotationEvent.model_validate_json(original.model_dump_json())
        assert rebuilt.payload.intent == "x"
        assert rebuilt.payload.signal_type == "dead_end"
        assert rebuilt.payload.context is not None
        assert rebuilt.payload.context["nested"]["b"] == [2, 3]


# =============================================================================
# Discriminated union — worker reads JSON, dispatches to right concrete type
# =============================================================================


class TestDiscriminatedUnion:
    """SPEC §11.4: events arrive at the Console worker as JSON; the worker
    parses them via discriminator on event_type."""

    def test_parse_tool_call_start(self) -> None:
        adapter: TypeAdapter[Event] = TypeAdapter(Event)
        data = {
            "event_id": "01970000-0000-7000-8000-000000000000",
            "event_type": "tool_call_start",
            "tenant_id": "t",
            "session_id": "s",
            "sequence_number": 1,
            "captured_at": "2026-05-19T16:42:03+00:00",
            "consent_token": "ct_test",
            "sdk_version": "0.2.0",
            "agent_runtime": "claude-code",
            "payload": {"tool_name": "t", "params": {}},
        }
        parsed = adapter.validate_python(data)
        assert isinstance(parsed, ToolCallStartEvent)

    def test_parse_annotation(self) -> None:
        adapter: TypeAdapter[Event] = TypeAdapter(Event)
        data = {
            "event_id": "01970000-0000-7000-8000-000000000001",
            "event_type": "annotation",
            "tenant_id": "t",
            "session_id": "s",
            "sequence_number": 2,
            "captured_at": "2026-05-19T16:42:03+00:00",
            "consent_token": "ct_test",
            "sdk_version": "0.2.0",
            "agent_runtime": "claude-code",
            "payload": {"intent": "x"},
        }
        parsed = adapter.validate_python(data)
        assert isinstance(parsed, AnnotationEvent)
        assert parsed.payload.intent == "x"

    def test_unknown_event_type_rejected(self) -> None:
        adapter: TypeAdapter[Event] = TypeAdapter(Event)
        data = {
            "event_id": "01970000-0000-7000-8000-000000000002",
            "event_type": "made_up_event",
            "tenant_id": "t",
            "session_id": "s",
            "sequence_number": 3,
            "captured_at": "2026-05-19T16:42:03+00:00",
            "consent_token": "ct_test",
            "sdk_version": "0.2.0",
            "agent_runtime": "claude-code",
            "payload": {},
        }
        with pytest.raises(ValidationError):
            adapter.validate_python(data)

    def test_parse_from_json_string(self) -> None:
        """Worker receives bytes/strings from the HTTP body; must parse cleanly."""
        adapter: TypeAdapter[Event] = TypeAdapter(Event)
        payload_json = json.dumps(
            {
                "event_id": "01970000-0000-7000-8000-000000000003",
                "event_type": "tool_call_error",
                "tenant_id": "t",
                "session_id": "s",
                "sequence_number": 4,
                "captured_at": "2026-05-19T16:42:03+00:00",
                "consent_token": "ct_test",
                "sdk_version": "0.2.0",
                "agent_runtime": "claude-code",
                "payload": {
                    "tool_name": "t",
                    "error_type": "TimeoutError",
                    "error_body": "...",
                },
            }
        )
        parsed = adapter.validate_json(payload_json)
        assert isinstance(parsed, ToolCallErrorEvent)
        assert parsed.payload.error_type == "TimeoutError"
