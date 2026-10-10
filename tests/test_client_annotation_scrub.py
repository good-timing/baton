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


def test_sync_trace_scrubs_its_proactive_annotation() -> None:
    sink = _CollectingSink()
    client = Client(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=_redact)
    with client.trace(tool_name="fetch", **{name: _TEXT[name] for name in _PROACTIVE}) as t:
        t.observed(result="ok")
    client.close()
    assert _annotation_fields(sink, _PROACTIVE) == {name: _REDACTED[name] for name in _PROACTIVE}


async def test_async_trace_scrubs_its_proactive_annotation() -> None:
    sink = _CollectingSink()
    client = AsyncClient(tenant_id="ten_abc", vendor_id="v1", sink=sink, scrubber=_redact)
    async with client.trace(tool_name="fetch", **{name: _TEXT[name] for name in _PROACTIVE}) as t:
        t.observed(result="ok")
    await client.aclose()
    assert _annotation_fields(sink, _PROACTIVE) == {name: _REDACTED[name] for name in _PROACTIVE}
