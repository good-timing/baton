"""The three tool list events (SPEC §11.4.5)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from baton.events import Event, ToolListEndEvent, ToolListErrorEvent, ToolListStartEvent

_ENVELOPE: dict[str, Any] = {
    "tenant_id": "ten_1",
    "vendor_id": "acme",
    "session_id": "sdk-1",
    "sequence_number": 1,
    "captured_at": "2026-01-01T00:00:00Z",
    "consent_token": "ct",
}

_PAYLOADS: dict[str, dict[str, Any]] = {
    "tool_list_start": {},
    "tool_list_end": {"count": 3, "duration_ms": 4},
    "tool_list_error": {"error_type": "RuntimeError", "error_body": "boom", "duration_ms": 4},
}


@pytest.mark.parametrize(
    ("event_type", "model"),
    [
        ("tool_list_start", ToolListStartEvent),
        ("tool_list_end", ToolListEndEvent),
        ("tool_list_error", ToolListErrorEvent),
    ],
)
def test_the_union_reads_each_type_as_its_own_model(event_type: str, model: type) -> None:
    wire = {**_ENVELOPE, "event_type": event_type, "payload": _PAYLOADS[event_type]}

    assert isinstance(TypeAdapter(Event).validate_python(wire), model)


@pytest.mark.parametrize("event_type", sorted(_PAYLOADS))
def test_no_payload_may_carry_the_list(event_type: str) -> None:
    payload = {**_PAYLOADS[event_type], "tools": [{"name": "lookup"}]}

    with pytest.raises(ValidationError) as refused:
        TypeAdapter(Event).validate_python(
            {**_ENVELOPE, "event_type": event_type, "payload": payload}
        )

    assert [(e["type"], e["loc"][-1]) for e in refused.value.errors()] == [
        ("extra_forbidden", "tools")
    ]


def test_the_end_event_must_say_how_many() -> None:
    with pytest.raises(ValidationError) as refused:
        ToolListEndEvent.model_validate({**_ENVELOPE, "payload": {"duration_ms": 4}})

    assert [(e["type"], e["loc"][-1]) for e in refused.value.errors()] == [("missing", "count")]


def test_a_server_with_no_known_list_handler_installs_without_the_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from baton._state import SessionCounter
    from baton.integrations._lifecycle import install_lifecycle_capture
    from baton.sinks import StdoutSink

    async def read_request(request_context: Any) -> Any:
        raise AssertionError("no handler was wrapped, so nothing calls this")

    install_lifecycle_capture(
        lambda: object(),
        tenant_id="ten_1",
        vendor_id="acme",
        consent_token="ct",
        sink=StdoutSink(),
        counter=SessionCounter(),
        scrubber=lambda value: value,
        resolve_principal_hook=None,
        read_request=read_request,
        read_access_token=lambda: None,
    )

    assert "no known request handler layout" in caplog.text
