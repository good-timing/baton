"""The library path scrubs annotation text (SPEC §11.2 item 2), as both adapters do."""

from __future__ import annotations

from typing import Any

from baton.client import AsyncClient, Client
from baton.events import Event
from baton.sinks import Sink

SECRET = "alice@acme.com"
FREE_TEXT_FIELDS = (
    "intent",
    "expected_outcome",
    "workflow",
    "suggested_improvement",
    "what_happened",
    "tool_name",
)


class _CollectingSink(Sink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def write(self, event: Event) -> None:
        self.events.append(event)

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(SECRET, "[REDACTED]")
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    return value


def _annotation_fields(sink: _CollectingSink, fields: tuple[str, ...]) -> dict[str, Any]:
    (annotation,) = [e for e in sink.events if e.event_type == "annotation"]
    return {name: getattr(annotation.payload, name) for name in fields}


_TEXT = {name: f"{name} for {SECRET}" for name in FREE_TEXT_FIELDS}
_REDACTED = {name: f"{name} for [REDACTED]" for name in FREE_TEXT_FIELDS}
_PROACTIVE = ("intent", "expected_outcome", "workflow")


def test_sync_annotate_scrubs_every_free_text_field() -> None:
    sink = _CollectingSink()
    client = Client(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=_redact)
    client.annotate(**_TEXT)
    client.close()
    assert _annotation_fields(sink, FREE_TEXT_FIELDS) == _REDACTED


async def test_async_annotate_scrubs_every_free_text_field() -> None:
    sink = _CollectingSink()
    client = AsyncClient(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=_redact)
    await client.annotate(**_TEXT)
    await client.aclose()
    assert _annotation_fields(sink, FREE_TEXT_FIELDS) == _REDACTED


_CALL_FIELD = {
    "intent": "call_intent",
    "expected_outcome": "call_expected",
    "workflow": "call_workflow",
}


def _start_fields(sink: _CollectingSink) -> dict[str, Any]:
    (start,) = [e for e in sink.events if e.event_type == "tool_call_start"]
    return {name: getattr(start.payload, field) for name, field in _CALL_FIELD.items()}


def test_sync_trace_scrubs_the_intent_on_its_call() -> None:
    sink = _CollectingSink()
    client = Client(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=_redact)
    with client.trace(tool_name="fetch", **{name: _TEXT[name] for name in _PROACTIVE}) as t:
        t.observed(result="ok")
    client.close()
    assert _start_fields(sink) == {name: _REDACTED[name] for name in _PROACTIVE}


async def test_async_trace_scrubs_the_intent_on_its_call() -> None:
    sink = _CollectingSink()
    client = AsyncClient(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=_redact)
    async with client.trace(tool_name="fetch", **{name: _TEXT[name] for name in _PROACTIVE}) as t:
        t.observed(result="ok")
    await client.aclose()
    assert _start_fields(sink) == {name: _REDACTED[name] for name in _PROACTIVE}


def test_a_scrubber_that_raises_on_the_intent_costs_the_field_not_the_call() -> None:
    def _raise_on_intent(value: Any) -> Any:
        if value == "secret goal":
            raise RuntimeError("scrubber broke")
        return value

    sink = _CollectingSink()
    client = Client(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=_raise_on_intent)
    with client.trace(tool_name="fetch", intent="secret goal", expected_outcome="a row") as t:
        t.observed(result="ok")
    client.close()
    (start,) = [e for e in sink.events if e.event_type == "tool_call_start"]
    assert start.payload.call_intent is None
    assert start.payload.call_expected == "a row"
    assert [e.event_type for e in sink.events] == ["tool_call_start", "tool_call_end"]
