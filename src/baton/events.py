"""Pydantic schemas for the Baton event stream per SPEC §11.4.

The SDK emits these events at the MCP transport boundary (via middleware) or
from direct library calls (``baton.Client`` / ``AsyncClient``). The collector
worker ingests them, stitches them into SignalPayloads per SPEC §11.5,
applies policy per SPEC §11.6, and dispatches.

Per CHARTER ADR-4 the event schema is the canonical wire format. Worker reads
JSON; concrete event class is selected via the ``event_type`` discriminator.

All concrete event classes share the same envelope (``_EventEnvelope``) and
differ only in their ``payload`` field's type. This keeps correlation logic
(SPEC §11.5) uniform — worker groups by ``(tenant_id, session_id)`` + sorts
by ``sequence_number`` regardless of event_type.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from uuid6 import uuid7

from baton import __version__

EventType = Literal[
    "tool_call_start",
    "tool_call_end",
    "tool_call_error",
    "annotation",
]


# =============================================================================
# Per-event-type payloads
# =============================================================================


class ToolCallStartPayload(BaseModel):
    """Emitted before the vendor handler runs. ``params`` is PII-scrubbed at
    emit-time per SPEC §7."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)


class ToolCallEndPayload(BaseModel):
    """Emitted after the vendor handler returns. ``result`` is PII-scrubbed."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    result: Any | None = None
    duration_ms: int | None = None


class ToolCallErrorPayload(BaseModel):
    """Emitted when the vendor handler raises. ``error_type`` is the exception
    class name; ``error_body`` is the exception message (PII-scrubbed)."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    error_type: str
    error_body: str
    duration_ms: int | None = None


class AnnotationPayload(BaseModel):
    """Agent-supplied context. All fields nullable per SPEC §5.1.1 — agent
    populates what it has. Proactive annotations typically populate
    ``intent``/``expected_outcome``/``workflow``; reactive annotations
    typically populate ``signal_type``/``suggested_improvement``."""

    model_config = ConfigDict(extra="forbid")

    intent: str | None = None
    expected_outcome: str | None = None
    signal_type: str | None = None
    workflow: str | None = None
    suggested_improvement: str | None = None
    context: dict[str, Any] | None = None


# =============================================================================
# Envelope shared by all event types
# =============================================================================


class _EventEnvelope(BaseModel):
    """Fields every Baton event carries. Concrete event classes (below)
    inherit this + add a ``event_type`` literal and typed ``payload``.

    ``consent_token`` is REQUIRED per SPEC §2.3 + §3.1 — the Console MUST
    reject any event missing it. v0 form: a single UUID granted at SDK init;
    v0.x will extend to per-end-user OAuth-scoped tokens (CHARTER ADR-1).

    ``vendor_id`` is REQUIRED — the wrapped vendor identifier (matches the
    SDK's ``VendorConfig.vendor_id`` / ``Client(vendor_id=...)``). For
    customer-mode tenants the Console uses ``(tenant_id, vendor_id)`` to
    group friction per wrapped vendor under a single customer; for
    vendor-mode tenants ``vendor_id`` matches ``tenants.vendor_id``. The
    Console rejects envelopes missing it (fail-loud per `tenant_type` design).
    """

    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid7)
    tenant_id: str
    vendor_id: str
    session_id: str
    sequence_number: int = Field(ge=0)
    captured_at: datetime
    consent_token: str
    sdk_version: str = __version__
    agent_runtime: str = "unknown"
    runtime_meta: dict[str, Any] | None = None
    """Runtime-supplied ``_meta`` envelope from the MCP request (SPEC §11.4).
    Per SPEC §11.5 the Console worker uses this to derive turn / cycle
    boundaries that are more precise than ``session_id`` alone (which is
    only the SDK-process lifetime, not a conversation turn). Examples:
    ``claudecode/toolUseId``, ``claudecode/sessionId``, ``progressToken``.
    Null when the host runtime didn't surface a meta or the adapter can't
    access it. PII-scrubbed if the vendor's scrubber covers metadata keys."""


# =============================================================================
# Concrete event classes
# =============================================================================


class ToolCallStartEvent(_EventEnvelope):
    event_type: Literal["tool_call_start"] = "tool_call_start"
    payload: ToolCallStartPayload


class ToolCallEndEvent(_EventEnvelope):
    event_type: Literal["tool_call_end"] = "tool_call_end"
    payload: ToolCallEndPayload


class ToolCallErrorEvent(_EventEnvelope):
    event_type: Literal["tool_call_error"] = "tool_call_error"
    payload: ToolCallErrorPayload


class AnnotationEvent(_EventEnvelope):
    event_type: Literal["annotation"] = "annotation"
    payload: AnnotationPayload


# =============================================================================
# Discriminated union — worker reads JSON, dispatches to concrete type
# =============================================================================

Event = Annotated[
    ToolCallStartEvent | ToolCallEndEvent | ToolCallErrorEvent | AnnotationEvent,
    Field(discriminator="event_type"),
]


__all__ = [
    "AnnotationEvent",
    "AnnotationPayload",
    "Event",
    "EventType",
    "ToolCallEndEvent",
    "ToolCallEndPayload",
    "ToolCallErrorEvent",
    "ToolCallErrorPayload",
    "ToolCallStartEvent",
    "ToolCallStartPayload",
]
