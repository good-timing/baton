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

from baton import __version__
from baton._uuid import uuid7

EventType = Literal[
    "tool_call_start",
    "tool_call_end",
    "tool_call_error",
    "annotation",
    "surface_snapshot",
]


# =============================================================================
# Per-event-type payloads
# =============================================================================


class ToolCallStartPayload(BaseModel):
    """Emitted before the vendor handler runs. ``params`` is PII-scrubbed at
    emit-time per SPEC §7.

    ``call_intent`` / ``call_expected`` / ``call_workflow`` are the values the
    SDK stripped from the injected ``user_goal`` / ``expected_result`` /
    ``overall_task`` params (``integrations._llm_text``); they ride as
    SIBLINGS of ``params`` — ``params`` stays exactly the vendor-visible
    arguments. ``call_intent``/``call_expected`` are call-scoped diagnostics;
    ``call_workflow`` is the task-label grouping key (console rung 3b, exact
    string continuity). ``intent_source`` records provenance
    (``"injected_param"``). All null when the params weren't used. The Console
    reads these off ``payload`` (``worker/correlate.py``, ``cycle.py``); kept
    in lockstep with the proxy's emitter output."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    call_intent: str | None = None
    call_expected: str | None = None
    call_workflow: str | None = None
    intent_source: str | None = None


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
    intent_source: str | None = None
    """Provenance for synthesised proactives — ``"injected_param"`` when this
    annotation was generated from a stripped ``user_goal``/``expected_result``
    param rather than a real annotation-tool call. Null for agent-authored
    annotations. Mirrors the proxy's ``enqueue_annotation`` output."""
    tool_name: str | None = None
    """The tool whose injected intent seeded this synthesised proactive. Null
    for agent-authored annotations."""


class SurfaceSnapshotPayload(BaseModel):
    """The vendor-true upstream surface (pre-injection) — mirrors baton-proxy's
    ``enqueue_surface_snapshot`` payload's top-level fields (see
    ``baton_proxy.emitter.Emitter.enqueue_surface_snapshot``) so the Console
    worker materializes both into the same ``vendor_surfaces`` table. Emitted
    at most once per observed ``surface_hash`` per process.

    ``tools`` excludes Baton's own injected tool(s) (e.g. the annotation
    tool) — those are recorded in ``seam_augmentations.injected_tools``
    instead, matching proxy's split. ``surface_hash`` is the identity change
    specs are authored against (proxy's ``base_surface_hash``); it must NOT
    include anything Baton adds, or toggling e.g. ``intent_param_mode`` would
    invalidate every recipe pinned to the vendor's real surface.

    ``seam_augmentations.intent_param`` carries plural ``names: list[str]``,
    now the same three names in both producers (``user_goal``,
    ``expected_result``, ``overall_task``) since baton-proxy gained the third.
    Older events still carry the shapes this field has had before — two names,
    or proxy's original singular ``name: str`` — so console-side consumers MUST
    keep handling all of them; see
    ``baton_console.dashboard.queries.build_surface_view``. The list is DATA,
    not shape: it must never feed ``surface_hash``, or adding a param would
    invalidate every recipe pinned to the vendor's real surface.
    """

    model_config = ConfigDict(extra="forbid")

    surface_hash: str
    server_info: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = None
    instructions: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    seam_augmentations: dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# Envelope shared by all event types
# =============================================================================


DEFAULT_CONSENT_TOKEN = "customer-consented"
"""What the SDK puts in ``consent_token`` when the vendor names no other value.

**The field stays on the wire and the customer stops carrying it.** SPEC §2.3
governs the envelope, not the config, so defaulting here changes nothing a
consumer sees: this is byte-for-byte the value the onboarding recipe has been
minting into ``BATON_CONSENT_TOKEN`` all along (``recipes.py``'s
``CONSENT_TOKEN``), now stated once instead of threaded through an environment
variable that reads to nobody.

**Why the field is kept rather than removed**, since a field nothing reads is
the obvious thing to cut: the collector's event schema is ``extra="forbid"``,
and both SDKs are published and sending this field. Dropping it costs SPEC, two
SDKs, two releases, regenerated cross-repo vectors and a collector that
tolerates the field through the overlap anyway — and re-adding a REQUIRED
envelope field later is precisely the change that stops being free once anyone
is installed. "No customers" is the argument for keeping it.

**This is not the consent surface.** What a server's users are told is a README
paragraph and an opt-out switch, not a constant nobody reads. CHARTER ADR-1's
per-end-user token lands on this field when it is due; the deployment model
this default describes is a builder instrumenting their own server.
"""


class PrincipalWire(BaseModel):
    """The principal AS EMITTED — the finished envelope value (SPEC §11.4).

    ⚠ **Not ``baton.Principal``, and the two are easy to confuse.** That one is
    what a vendor's ``resolve_principal`` hook HANDS US: a raw subject, an
    optional issuer, optional PII that never leaves the payload tier. This one
    is what we PUT ON THE WIRE after resolving and deriving it — the raw value
    is gone by the time this is built, and nothing here is ever the input to a
    hash. One is the question, this is the answer.

    **All three members are REQUIRED, and that is the guarantee the object
    exists to give.** A producer emits the whole thing or omits ``principal``
    entirely; a partial object is malformed, not a degraded reading. So there
    is no conformant event carrying an ``id`` whose ``form`` a consumer has to
    guess, and none carrying a ``source`` for an identity nobody resolved. That
    binding is structural here precisely because its predecessor — a scheme
    prefix plus a paragraph of prose — was not.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    """The value: an HMAC pseudonym in ``"hashed"`` mode, the principal
    verbatim in ``"raw"`` mode. Which one is ``form``, and it is NEVER the
    value's shape — a real OIDC subject (``mailto:``, ``acct:``, ``urn:``,
    ``https:``) reads as a scheme-tagged pseudonym to anything testing for
    "letters then a colon"."""

    source: str
    """WHERE it came from: ``"attested"`` (a verified token's ``sub``) or
    ``"asserted"`` (a vendor's own resolver, which nothing in the protocol
    checks). Both are legitimate and asserted is not a degraded attested —
    it is the only identity mechanism that exists on stdio."""

    form: str
    """WHAT it is: ``"hashed"`` or ``"raw"``. The privacy classification, and
    the only thing a consumer may classify on."""

    # ⚠ Deliberately `str`, not `Literal`, on BOTH members — the same decision
    # `transport_observed` records and the collector's ingest makes on the
    # columns behind this. A fourth source is already foreseen (alias-derived,
    # from N7/E6), and a `Literal` would make this producer unable to emit a
    # value SPEC registers later without a release. Worse, it would raise at
    # the emit boundary, which SPEC §11.2 requires to fail OPEN: an identity
    # read may never cost a tool call. The safety lives in the consumer rules
    # stated positively — trust only exactly "attested", treat anything but
    # exactly "hashed" as personal data — so an unregistered value fails safe
    # without anything having to reject it.


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
    principal: PrincipalWire | None = None
    """Who the vendor resolved behind this event — a person, a service account
    or an organisation (SPEC §11.4).

    **Absent as a whole whenever no identity resolved**, which is the common
    case and never an error: no auth on the request, stdio with no hook
    configured, or hashed mode with no key. Never a partial object — see
    ``PrincipalWire``.

    Was the flat field ``user_id`` until 0.8.6, then the flat ``principal_id``,
    and became this object at the release SPEC §13 leaves unnumbered."""
    call_id: str | None = None
    """The minted per-call correlation key (SPEC §11.4, OPTIONAL + nullable).

    The SAME value on a tool call's ``tool_call_start`` and its
    ``tool_call_end`` / ``tool_call_error``, so a worker pairs the two legs on
    an identifier this producer controls rather than inferring the pairing.
    Consumers key tier 1 on ``(call_id, tool_name)`` (SPEC §11.5.4), not on the
    id alone.

    Null on every event emitted before this field existed, and on the
    ``annotation`` event, which SPEC defines no ``call_id`` for — the field is
    specified for a tool call's legs, and putting one on an annotation would
    invent semantics no spec text defines. Null is never an error.

    Minted as a bare opaque UUID string in a local variable inside the scope
    that emits both legs — per-call by construction and correct across
    processes. Never derived from the JSON-RPC request id, which restarts at 1
    per connection. It says WHICH CALL, never WHO; the principal is ``principal.id``.
    """
    transport_observed: str | None = None
    """What this producer OBSERVED beneath the call, never what it concluded
    (SPEC §11.4, OPTIONAL + nullable).

    ``"http"`` — an HTTP request object was reachable from the call's context,
    so one process MAY be serving many callers. ``"no-http-request"`` — a live
    MCP request with no HTTP behind it: stdio or in-memory, where one process
    is one caller. ``"read-failed"`` — this producer's own read of the
    transport raised, which says nothing about the deployment and everything
    about us. Null where we did not look: the library API, which has no MCP
    transport at all.

    **A raised read MUST become ``"read-failed"``, never ``"no-http-request"``.**
    That value asserts a fact about the customer's deployment, and an
    unreadable context is not that fact. Folding the two ships one of our bugs
    as evidence that grouping is safe — the exact merge SPEC §3.4 rung 5 and
    this field exist to prevent. The official adapter is where this bites:
    ``_extract_headers_from_context`` already catches ``AttributeError`` and
    returns the same ``None`` as a genuine absence, so this read keys on
    ``request_context.request`` directly and maps ONLY ``None``.

    Deliberately a plain ``str`` and not an enum: SPEC registers new values
    without a major version, and a consumer must tolerate one it does not know
    rather than reject the event. It says WHAT WE SAW, never who or where — no
    address, no host, no port — so it carries no security property and is not
    for authorization.
    """
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


class SurfaceSnapshotEvent(_EventEnvelope):
    event_type: Literal["surface_snapshot"] = "surface_snapshot"
    payload: SurfaceSnapshotPayload


# =============================================================================
# Discriminated union — worker reads JSON, dispatches to concrete type
# =============================================================================

Event = Annotated[
    ToolCallStartEvent
    | ToolCallEndEvent
    | ToolCallErrorEvent
    | AnnotationEvent
    | SurfaceSnapshotEvent,
    Field(discriminator="event_type"),
]


__all__ = [
    "AnnotationEvent",
    "AnnotationPayload",
    "Event",
    "EventType",
    "PrincipalWire",
    "SurfaceSnapshotEvent",
    "SurfaceSnapshotPayload",
    "ToolCallEndEvent",
    "ToolCallEndPayload",
    "ToolCallErrorEvent",
    "ToolCallErrorPayload",
    "ToolCallStartEvent",
    "ToolCallStartPayload",
]
