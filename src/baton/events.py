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
    # The RESOURCE and PROMPT lifecycles (SPEC §11.4.4). Twelve types, and
    # their schema of record is HERE because this module is what
    # ``baton-spec/scripts/generate.py`` exports the schema from — not because
    # this SDK emits them. It does not: ``baton-proxy`` is the only producer,
    # and it has been emitting all twelve against no schema at all.
    "resource_list_start",
    "resource_list_end",
    "resource_list_error",
    "resource_read_start",
    "resource_read_end",
    "resource_read_error",
    "prompt_list_start",
    "prompt_list_end",
    "prompt_list_error",
    "prompt_get_start",
    "prompt_get_end",
    "prompt_get_error",
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
    result_capture: str | None = None
    """SPEC §11.4: the producer declares it WITHHELD result-derived data.

    Absent means captured, so no consumer needs a version table to read
    events that predate the member. Registered value: ``"off"``.

    ``str``, not ``Literal["off"]``, and not a bool: SPEC reserves a second
    registered value for the content ladder's partial rung, and a ``Literal``
    would make this producer unable to emit a value SPEC registers later —
    turning a forward-compatible envelope into a ``ValidationError`` on a path
    §11.2 requires to fail open. Same rule ``principal.source`` follows.

    Declared here, unused here: the config knob and the withholding itself
    are a separate change. This is only the wire schema PERMITTING the member,
    which has to be published before any producer emits it — ``extra="forbid"``
    above is why.
    """


class ToolCallErrorPayload(BaseModel):
    """Emitted when the call FAILED, which MCP expresses two ways (SPEC §11.4.3).

    **RAISE** — the vendor handler raised. ``error_type`` is the exception class
    name, ``error_body`` its message (PII-scrubbed), and ``result`` is None:
    there is no result object to record.

    **RETURN** — the handler returned normally and the result carried MCP's
    error flag, which rides a 200 response rather than a JSON-RPC error.
    ``error_type`` is ``"tool_error"``, ``error_body`` is the reason unwrapped
    from the result's ``content`` text parts, and ``result`` carries the full
    envelope, PII-scrubbed and NOT unwrapped the way ``tool_call_end.result``
    unwraps to the developer's return — the envelope is what holds the flag and
    the reason.

    ``result`` exists so reclassifying the RETURN shape off ``tool_call_end``
    does not move a structured body into a flat truncated string.
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    error_type: str
    error_body: str
    duration_ms: int | None = None
    result: Any | None = None
    result_capture: str | None = None
    """SPEC §11.4, and it rides this payload too because the RETURN shape
    withholds ``result`` as well.

    ⚠ On that shape ``error_body`` is unwrapped FROM the result, so it is
    withheld — but it is a REQUIRED member here and cannot be dropped, so it
    is emitted as ``""``. An empty ``error_body`` is therefore ambiguous on
    its own, and this member is what tells "withheld by policy" from "the
    failure carried no message". See ``ToolCallEndPayload.result_capture``
    for why it is a string.
    """
    failure_kind: str | None = None
    """SPEC §11.4.3: the producer NAMES a failure it manufactured ABOVE the
    vendor's handler, instead of leaving a consumer to pattern-match prose.

    Registered values, each declaring which of this subsection's two shapes it
    belongs to — which is the whole reason the member exists, since
    ``error_type`` is already the RAISE/RETURN discriminator and this class is
    neither:

    ===========================  ==========================================
    value                        shape
    ===========================  ==========================================
    ``unknown_tool``             the handler never ran: no such tool
    ``tool_disabled``            the handler never ran: the tool is off
    ``invalid_argument``         the handler never ran: arguments rejected
    ``output_schema_mismatch``   the handler RETURNED and our conversion of
                                 its output rejected it
    ===========================  ==========================================

    **Absent means the vendor's handler ran and spoke for itself** — a tool
    that returned an error, or raised one. Absent is therefore correct both for
    a producer predating this member and for every failure the vendor's own
    code produced, so no consumer needs a version table.

    ⚠ **Absent and null are equivalent, and a consumer MUST NOT test for the
    KEY.** Same mechanical reason ``result_capture`` carries the same rule:
    ``model_dump(mode="json")`` with no ``exclude_none`` puts
    ``"failure_kind": null`` on the wire. Read the VALUE.

    ⚠ **This payload only.** The output-schema case files ``tool_call_end``
    today, and that false success is what naming this corrects — so after the
    fix nothing carries a ``failure_kind`` on a success payload. Unlike
    ``result_capture``, which rides both.

    ⚠ **The first three are NOT result-derived** — the handler never ran, so
    nothing it returned exists. Their ``error_body`` STAYS under
    ``result_capture: "off"`` (scrubbed: the argument-rejection message can
    echo argument values). ``output_schema_mismatch``'s message describes what
    the tool RETURNED, so it GOES. A producer sorts these by whether its inner
    tool wrapper fired, not by inspecting the result object — all four arrive
    as the same shape.

    ``str``, not a ``Literal`` and not a closed enum in the generated schema,
    for the reason ``result_capture`` is: a producer must stay able to emit a
    value SPEC registers later rather than raise on a path §11.2 requires to
    fail open.

    Declared here, unused here: emitting it needs a second capture seam above
    the executor, which is a separate change. This is the wire schema
    PERMITTING the member, which has to be published before any producer emits
    it — ``extra="forbid"`` above is why.
    """


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
    keep handling all of them — a consumer building a surface view reads every
    shape, not just the current one. The list is DATA,
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

    model_config = ConfigDict(extra="forbid", frozen=True)
    """``frozen`` for two reasons beyond immutability being right here. It
    makes the model HASHABLE, so a test can put emitted principals straight
    into a set instead of freezing dicts into tuples and thawing them back —
    and comparing whole models is what upgrades those assertions from "the
    dict matched" to "the dict matched AND is a conformant ``PrincipalWire``".
    And this value is resolved once per call and attached to every event of
    that call, so nothing downstream has any business editing one event's
    copy."""

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
# Resource and prompt lifecycle payloads (SPEC §11.4.4)
# =============================================================================
#
# ⚠ **Transcribed from ``baton_proxy.emitter``, field for field, because that
# producer SHIPPED FIRST.** All twelve types have been reaching the Console in
# production — whose ingest ``EventType`` lists every one of them — against no
# schema definition anywhere: not here, not in ``events.schema.json``, not in
# SPEC. So these models do not design a shape, they RECORD one, and where the
# proxy's choice looks odd the oddity is preserved and annotated rather than
# corrected. Correcting it here would make the schema of record disagree with
# the only producer there is.
#
# ⚠ **No ``result_capture`` on any of the twelve, and that is not an
# oversight.** SPEC §11.4 scopes the member to a TOOL's result, and none of
# these payloads carries a body: a resource read records its URI and its
# timing, never its content; a list records a count. There is nothing for
# ``"off"`` to withhold, so an SDK matching this shape needs no withhold
# extension — see ``response_capture_switch.md`` §"The rule is scoped to TOOL
# results", which corrects an earlier claim that it did.
#
# ⚠ **The ``*_start`` payloads DO carry caller data**, in ``params``, and it is
# PII-scrubbed at emit time like every other payload (SPEC §7, §11.2.2).
# ``result_capture`` says nothing about that: §11.4 states outright that it is
# not a statement about request data.
#
# ⚠ **There is no ``result``, and no returned-flag shape, on any of the six
# error payloads.** ``CallToolResult``'s error flag is a TOOL concept: a
# failing resource read or prompt get comes back as a JSON-RPC error, so these
# have only §11.4.3's RAISE analogue and that subsection's ``error_type``
# discriminator has no counterpart here. ``_emit_call_error`` in ``proxy.py``
# says the same thing from the producer's side — ``result`` "rides the tool
# lane only".


class ResourceListStartPayload(BaseModel):
    """Emitted before a ``resources/list`` reaches the upstream server.

    Deliberately EMPTY. A list request carries no subject — the proxy passes
    ``payload={}`` — and the envelope already names the session, the tenant and
    the vendor. A consumer counting list traffic has everything it needs
    without a member here."""

    model_config = ConfigDict(extra="forbid")


class ResourceListEndPayload(BaseModel):
    """Emitted after ``resources/list`` returns.

    ⚠ ``count`` counts the ``resources`` array ALONE. Resource TEMPLATES are a
    separate MCP method (``resources/templates/list``) with its own result
    array, and the proxy does not add them in (``proxy.py``'s
    ``_emit_call_end``: ``len((result or {}).get("resources", []))``). So a
    server exposing only templates reports ``count: 0`` here, correctly — and a
    consumer must not read this as "the server has no resources"."""

    model_config = ConfigDict(extra="forbid")

    count: int
    duration_ms: int | None = None


class ResourceListErrorPayload(BaseModel):
    """Emitted when ``resources/list`` failed.

    ``error_type`` is an UNCONSTRAINED string and the two producers spell it
    differently on purpose — see ``ResourceReadErrorPayload``, which carries
    the full note for all six error payloads."""

    model_config = ConfigDict(extra="forbid")

    error_type: str
    error_body: str
    duration_ms: int | None = None


class ResourceReadStartPayload(BaseModel):
    """Emitted before a ``resources/read`` reaches the upstream server.

    ⚠ ``uri`` is ALSO inside ``params``. The proxy builds ``params`` by
    removing ``_meta`` from the request's params and nothing else
    (``proxy.py``: ``{k: v for k, v in params.items() if k != "_meta"}``), and
    ``uri`` is one of them — so the subject appears twice, once in its own
    member and once in the bag. Recorded rather than deduplicated: the
    dedicated member is what a consumer should read, the bag is what the caller
    actually sent, and a producer that stripped ``uri`` from ``params`` would
    stop being able to say that."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    params: dict[str, Any] | None = None
    """The caller's own request params, PII-scrubbed (SPEC §7). ``None`` where
    the request carried nothing but ``_meta``."""


class ResourceReadEndPayload(BaseModel):
    """Emitted after ``resources/read`` returns.

    ⚠ **No content member, and that is the design rather than a gap.** The
    resource BODY is customer data of exactly the kind
    ``response_capture_switch.md`` exists to keep off the wire, and the only
    producer there is has never sent it. Recording the URI and the timing is
    what makes a failing or slow read visible without the body."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    duration_ms: int | None = None


class ResourceReadErrorPayload(BaseModel):
    """Emitted when ``resources/read`` failed.

    ⚠ **``error_type`` is an unconstrained string and the producers do NOT
    agree on how to spell it** — stated here, for all six error payloads,
    rather than left for a consumer to discover. ``baton-proxy`` reads the
    WIRE, so what it holds is the upstream's JSON-RPC error and it files the
    numeric ``code`` as a string (``"-32002"``). An in-process SDK sensor holds
    a live exception instead and files its class name, the way §11.4.3's RAISE
    shape already does for tools. Both are legal — the schema constrains
    neither, and §11.4.3 says outright that this member separates SHAPES and is
    not a closed set of values — but a consumer MUST NOT read one producer's
    spelling as the vocabulary.

    ⚠ **``error_body`` is KEPT whatever the capture mode.** It is a failed
    FETCH's message, not anything a resource returned, so nothing here is
    result-derived. §11.4.3's provenance rule put the proxy's own
    upstream-error analogue on the same footing."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    error_type: str
    error_body: str
    duration_ms: int | None = None


class PromptListStartPayload(BaseModel):
    """Emitted before a ``prompts/list`` reaches the upstream server. Empty,
    for the reason ``ResourceListStartPayload`` carries."""

    model_config = ConfigDict(extra="forbid")


class PromptListEndPayload(BaseModel):
    """Emitted after ``prompts/list`` returns. ``count`` counts the ``prompts``
    array."""

    model_config = ConfigDict(extra="forbid")

    count: int
    duration_ms: int | None = None


class PromptListErrorPayload(BaseModel):
    """Emitted when ``prompts/list`` failed. See
    ``ResourceReadErrorPayload`` for how ``error_type`` is spelled."""

    model_config = ConfigDict(extra="forbid")

    error_type: str
    error_body: str
    duration_ms: int | None = None


class PromptGetStartPayload(BaseModel):
    """Emitted before a ``prompts/get`` reaches the upstream server.

    ⚠ ``params`` is the request's ``arguments`` member alone, NOT the whole
    params bag — which is the opposite of ``ResourceReadStartPayload``'s
    choice, measured in ``proxy.py`` (``params=params.get("arguments")``
    against the resource path's dict comprehension). The two are inconsistent
    in the only producer that exists; the inconsistency is recorded here so a
    second producer copies the shape rather than guessing at a rule, and so a
    consumer does not expect ``name`` inside ``params`` the way ``uri`` does
    appear there."""

    model_config = ConfigDict(extra="forbid")

    name: str
    params: dict[str, Any] | None = None
    """The prompt's arguments, PII-scrubbed (SPEC §7). ``None`` where the
    request supplied none."""


class PromptGetEndPayload(BaseModel):
    """Emitted after ``prompts/get`` returns.

    ⚠ **No rendered messages**, for the reason ``ResourceReadEndPayload``
    gives. A prompt's rendered text is authored by the SERVER rather than
    fetched by a tool, so whether it is customer content at all is a decision
    nobody has taken — and the standing answer, from the only producer, is that
    it does not egress."""

    model_config = ConfigDict(extra="forbid")

    name: str
    duration_ms: int | None = None


class PromptGetErrorPayload(BaseModel):
    """Emitted when ``prompts/get`` failed. See ``ResourceReadErrorPayload``
    for how ``error_type`` is spelled and why ``error_body`` is kept."""

    model_config = ConfigDict(extra="forbid")

    name: str
    error_type: str
    error_body: str
    duration_ms: int | None = None


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


# ⚠ The twelve lifecycle events. They carry the SAME envelope as the five
# above — ``_EventEnvelope`` — which is what lets one collector endpoint accept
# all seventeen and one worker order them on ``(session_id,
# sequence_number)``. ``call_id``, ``principal`` and ``transport_observed``
# stay OPTIONAL and the only producer sends none of them on these types
# (``baton_proxy.emitter._enqueue`` stamps no ``call_id`` or
# ``transport_observed`` on any event, and the twelve enqueue methods take no
# ``principal``), so a consumer pairing a start with its end has only §11.5.4's
# FIFO floor here. Recorded, not fixed: a producer MAY mint a ``call_id``, and
# the first one that does needs no schema change.


class ResourceListStartEvent(_EventEnvelope):
    event_type: Literal["resource_list_start"] = "resource_list_start"
    payload: ResourceListStartPayload


class ResourceListEndEvent(_EventEnvelope):
    event_type: Literal["resource_list_end"] = "resource_list_end"
    payload: ResourceListEndPayload


class ResourceListErrorEvent(_EventEnvelope):
    event_type: Literal["resource_list_error"] = "resource_list_error"
    payload: ResourceListErrorPayload


class ResourceReadStartEvent(_EventEnvelope):
    event_type: Literal["resource_read_start"] = "resource_read_start"
    payload: ResourceReadStartPayload


class ResourceReadEndEvent(_EventEnvelope):
    event_type: Literal["resource_read_end"] = "resource_read_end"
    payload: ResourceReadEndPayload


class ResourceReadErrorEvent(_EventEnvelope):
    event_type: Literal["resource_read_error"] = "resource_read_error"
    payload: ResourceReadErrorPayload


class PromptListStartEvent(_EventEnvelope):
    event_type: Literal["prompt_list_start"] = "prompt_list_start"
    payload: PromptListStartPayload


class PromptListEndEvent(_EventEnvelope):
    event_type: Literal["prompt_list_end"] = "prompt_list_end"
    payload: PromptListEndPayload


class PromptListErrorEvent(_EventEnvelope):
    event_type: Literal["prompt_list_error"] = "prompt_list_error"
    payload: PromptListErrorPayload


class PromptGetStartEvent(_EventEnvelope):
    event_type: Literal["prompt_get_start"] = "prompt_get_start"
    payload: PromptGetStartPayload


class PromptGetEndEvent(_EventEnvelope):
    event_type: Literal["prompt_get_end"] = "prompt_get_end"
    payload: PromptGetEndPayload


class PromptGetErrorEvent(_EventEnvelope):
    event_type: Literal["prompt_get_error"] = "prompt_get_error"
    payload: PromptGetErrorPayload


# =============================================================================
# Discriminated union — worker reads JSON, dispatches to concrete type
# =============================================================================

Event = Annotated[
    ToolCallStartEvent
    | ToolCallEndEvent
    | ToolCallErrorEvent
    | AnnotationEvent
    | SurfaceSnapshotEvent
    | ResourceListStartEvent
    | ResourceListEndEvent
    | ResourceListErrorEvent
    | ResourceReadStartEvent
    | ResourceReadEndEvent
    | ResourceReadErrorEvent
    | PromptListStartEvent
    | PromptListEndEvent
    | PromptListErrorEvent
    | PromptGetStartEvent
    | PromptGetEndEvent
    | PromptGetErrorEvent,
    Field(discriminator="event_type"),
]


__all__ = [
    "AnnotationEvent",
    "AnnotationPayload",
    "Event",
    "EventType",
    "PrincipalWire",
    "PromptGetEndEvent",
    "PromptGetEndPayload",
    "PromptGetErrorEvent",
    "PromptGetErrorPayload",
    "PromptGetStartEvent",
    "PromptGetStartPayload",
    "PromptListEndEvent",
    "PromptListEndPayload",
    "PromptListErrorEvent",
    "PromptListErrorPayload",
    "PromptListStartEvent",
    "PromptListStartPayload",
    "ResourceListEndEvent",
    "ResourceListEndPayload",
    "ResourceListErrorEvent",
    "ResourceListErrorPayload",
    "ResourceListStartEvent",
    "ResourceListStartPayload",
    "ResourceReadEndEvent",
    "ResourceReadEndPayload",
    "ResourceReadErrorEvent",
    "ResourceReadErrorPayload",
    "ResourceReadStartEvent",
    "ResourceReadStartPayload",
    "SurfaceSnapshotEvent",
    "SurfaceSnapshotPayload",
    "ToolCallEndEvent",
    "ToolCallEndPayload",
    "ToolCallErrorEvent",
    "ToolCallErrorPayload",
    "ToolCallStartEvent",
    "ToolCallStartPayload",
]
