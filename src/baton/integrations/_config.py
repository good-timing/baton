"""Shared VendorConfig used by both adapter ``install_baton`` functions."""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from baton._dsn import VENDOR_ID_PATTERN as _VENDOR_ID_PATTERN
from baton._dsn import parse_dsn, resolve_dsn, select_dsn
from baton.events import DEFAULT_CONSENT_TOKEN
from baton.integrations.identity_adapter import USER_ID_MODES
from baton.sinks import HttpSink, Sink, StdoutSink

logger = logging.getLogger(__name__)

# ``_VENDOR_ID_PATTERN`` is imported, not defined here, because a DSN's server
# segment IS a vendor_id and both rules have to be one object. Restating the
# ceiling as a second regex is how the two drift; this repo has already costed
# that number wrong twice.

# Per-tool intent-param injection modes (mirrors baton-proxy's BATON_INTENT_PARAM).
_INTENT_PARAM_MODES: frozenset[str] = frozenset({"optional", "required", "off"})
_PROACTIVE_MODES: frozenset[str] = frozenset({"on", "off"})


@dataclass(frozen=True)
class SessionResolutionContext:
    """Normalized input to ``VendorConfig.resolve_session_id``.

    Deliberately does not carry the raw SDK ``Context`` object — the
    official ``mcp`` and standalone ``fastmcp`` libraries expose different,
    adapter-specific ``Context`` types. This shape is what's already
    extracted for both adapters (headers, meta), so one hook works
    unmodified regardless of which adapter a vendor is on.
    """

    headers: Mapping[str, str] | None
    meta: dict[str, Any] | None
    tool_name: str
    arguments: dict[str, Any]


ResolveSessionIdHook = Callable[[SessionResolutionContext], "Awaitable[str | None] | str | None"]


async def resolve_via_hook(
    hook: ResolveSessionIdHook, context: SessionResolutionContext
) -> str | None:
    """Call a vendor's ``resolve_session_id`` hook and normalize its result.

    Never raises — an exception is logged and treated as a miss so the
    caller falls through to the SPEC §3.4 ladder unchanged. Accepts sync or
    async hooks (mirrors ``VendorConfig.scrubber``'s calling convention).
    """
    try:
        result = hook(context)
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        logger.warning("baton: resolve_session_id hook raised; falling through", exc_info=True)
        return None
    return result if isinstance(result, str) and result else None


def _resolve_tenant_id(explicit: str | None, vendor_id: str) -> str:
    """``tenant_id`` per SPEC §11.4: explicit → ``BATON_TENANT_ID`` → ``vendor_id``.

    The ``vendor_id`` tail is a migration shim for this repo's own fixtures, not
    a supported configuration: it reproduces exactly the collapse the split
    exists to end, so it is the branch to delete once the recipe emits the var.
    """
    if explicit:
        return explicit
    from_env = os.environ.get("BATON_TENANT_ID")
    if from_env:
        return from_env
    return vendor_id


def _resolve_user_id_hmac_key(explicit: bytes | str | None) -> bytes | None:
    """``user_id_hmac_key``: explicit → ``BATON_USER_ID_HMAC_KEY`` → ``None``.

    The env var is the contract baton-proxy and baton-extmcp have honoured
    since 0.5.0 and the one the console's setup string names, so it keeps
    working here unchanged. The explicit field is additive, for a vendor whose
    secrets arrive from a manager rather than the environment.

    ``None`` is a supported state, not an error: it means hashed-mode identity
    is off and events emit without ``user_id``.
    """
    if explicit is not None:
        # A ``str`` is encoded rather than refused, because the env path has
        # always taken one and a vendor moving a working secret from
        # ``BATON_USER_ID_HMAC_KEY`` into the field would otherwise hit
        # ``hmac.new``'s "expected bytes" TypeError — and only in the
        # deployment shape this field exists for (HTTP + OAuth), so never in
        # their local testing. Same secret, same bytes, either way.
        return explicit.encode("utf-8") if isinstance(explicit, str) else explicit
    from_env = os.environ.get("BATON_USER_ID_HMAC_KEY")
    return from_env.encode("utf-8") if from_env else None


@dataclass(kw_only=True)
class VendorConfig:
    """Vendor-side configuration for ``install_baton``.

    **Keyword-only, since 0.8.1.** ``VendorConfig("acme", "Acme", ...)`` now
    raises ``TypeError`` at construction instead of binding by position.

    Positional construction shipped a silent mis-bind TWICE. At 0.7.0 a
    ``Sink`` object landed in ``consent_token`` and rode onto the wire (0.7.1
    is the release that FIXED it); at
    0.8.0 ``default_agent_runtime`` was removed from slot 6 and two fields were
    inserted before ``resolve_session_id``, so a 0.7.2-shaped call put the
    string ``"unknown"`` in ``scrubber`` — a non-callable, constructed without
    complaint, failing far from the call site if at all. Both releases promised
    the opposite in their notes, and the guard written after the first one
    (``tests/test_tenant_vendor_split.py``) asserted the first four slots only,
    so it was green against the second.

    The promise is therefore retired rather than re-made: field ORDER is no
    longer public API, and there are now no positional slots to shift. That
    trade is deliberate — it costs the ability to construct this tersely and
    buys the ability to add, remove and reorder fields without a silent
    mis-bind, which a 13-field config will want more than once more."""

    vendor_id: str = ""
    """Short stable identifier for the vendor (e.g., ``"acme"``,
    ``"example-vendor"``). Becomes the default annotation tool name prefix
    (``{vendor_id}_annotate``); must match the cross-runtime tool-name pattern.

    Required unless a ``dsn`` supplies it — the DSN's last path segment is
    this value, and it is what the key is BOUND to."""

    vendor_display_name: str = ""
    """Human-readable vendor name used in server instructions, annotation
    tool description, and any LLM-facing strings. Whitelabel obligation
    (SPEC §5.4): no Baton-branded strings reach the calling agent.

    Defaults to the DSN's server segment verbatim when a ``dsn`` is given and
    this is not. Verbatim rather than prettified: this string reaches the
    calling agent, so inventing a capitalisation the vendor never chose would
    put a fabricated name in front of their users."""

    consent_token: str = DEFAULT_CONSENT_TOKEN
    """End-user consent token attached to every emitted event per SPEC §2.3 +
    §3.1 (the consumer of the events MUST reject events missing it).

    **Defaulted, so the customer never has to carry it** — see
    ``baton.events.DEFAULT_CONSENT_TOKEN`` for why the field is kept on the
    wire regardless. Passing ``""`` explicitly still raises: a value the vendor
    deliberately emptied is a mistake, not a request for the default.
    CHARTER ADR-1's per-end-user OAuth-scoped tokens land on this field."""

    sink: Sink | None = None
    """Where events go. ``None`` (the default) means the SDK picks: an
    ``HttpSink`` built from ``dsn`` when there is one, otherwise ``StdoutSink``
    — zero-config dev mode, writing JSON Lines to stderr. Pass an ``HttpSink``
    to ship to a collector, ``FileSink`` to capture for later analysis, or
    ``MultiSink`` to fan out (e.g., stdout + http during development).

    Explicit and ``dsn`` together raise, rather than one quietly winning."""

    annotation_tool_name: str | None = None
    """Optional override for the annotation tool name. Default is
    ``{vendor_id}_annotate``."""

    scrubber: Callable[[Any], Any] | None = None
    """PII scrubber per SPEC §7. Default (None) uses ``baton.scrub.Scrubber``
    — recursive walker with email/Bearer/sk-*/AKIA*/JWT/CC-Luhn/phone
    patterns + field-name overrides on by default. Pass
    ``baton.scrub.identity_scrub`` to opt out, or supply your own."""

    intent_param_mode: str = "optional"
    """Per-tool intent-param injection (mirrors baton-extmcp's vendor-neutral
    naming). ``"optional"`` (default) injects ``user_goal``/``expected_result``
    string params on every wrapped tool's input schema; ``"required"`` also
    adds ``user_goal`` to each tool's ``required`` list (``expected_result``
    stays optional regardless); ``"off"`` disables injection. Both params are
    stripped before the vendor handler runs, so the tool never sees them. This
    is what captures intent on runtimes that drop ``instructions`` (notably
    Claude Desktop) — where the annotation tool alone yields nothing."""

    proactive_mode: str = "off"
    """Whether the server instructions ask the agent to file a *proactive*
    annotation before each tool call. ``"off"`` (default) removes that request;
    ``"on"`` restores it.

    Off by default because the injected params supersede it: ``user_goal``,
    ``expected_result`` and ``overall_task`` ride every ``tool_call_start`` as
    ``call_intent``/``call_expected``/``call_workflow``, so a pre-call
    annotation carries no field the call event doesn't — while costing a full
    extra inference turn per tool call (measured 2x: 4 annotation calls serving
    4 tool calls). It also captures worse: the annotation path was measured at
    R=0.135 coverage vs the params' 1.000, and supplies conversation-scoped
    umbrella task labels where the param supplies per-task ones.

    **This never disables reactive annotation.** The tool stays on the surface,
    the friction clauses of the instructions stay verbatim, and the SDK's own
    synthesized proactive (built from the injected params, no agent turn) still
    fires — so ``intent``/``expected_outcome``/``workflow`` remain populated on
    the wire. Only the agent-initiated pre-call annotation goes away.

    Set ``"on"`` when ``intent_param_mode="off"``: with neither, nothing
    captures intent. The two are alternative intent channels, not additive —
    running both also makes two competing ``workflow`` labels that a consumer
    has to arbitrate."""

    user_id_mode: str = "hashed"
    """How an authenticated end-user principal reaches the wire (SPEC §11.4
    ``user_id``). ``"hashed"`` (default) emits ``h1:<hex>`` — an HMAC computed
    in this process, so the collector only ever sees the pseudonym. ``"raw"``
    emits the subject verbatim.

    **``"raw"`` puts real end-user identity in the collector's database.** It
    is the right choice for a vendor instrumenting a server whose users are
    themselves, or one with no residency obligation who would rather read a
    name than a hash — and the wrong choice by default, which is why it is not
    the default. On a multi-tenant vendor server the principals are the
    VENDOR's customers, and shipping their identities to a third party is a
    decision only that vendor can make.

    Hashed mode needs ``user_id_hmac_key``; raw mode needs nothing. The two are
    distinguishable on the wire without a second field, because a hashed value
    always carries the ``h1:`` scheme prefix."""

    user_id_hmac_key: bytes | str | None = None
    """Secret keying the ``user_id`` HMAC in ``"hashed"`` mode.

    Resolved explicit → ``BATON_USER_ID_HMAC_KEY`` → ``None``. Unset means
    hashed identity is fail-open-skipped: ``user_id`` is dropped, events still
    emit, and it is logged once. ``user_id`` is additive analytics — never a
    consent or authorization gate.

    **The vendor generates and holds this; Baton never sees it.** That is what
    makes the pseudonym real: if the collector held the key it could hash a
    list of candidate identities and reverse the column, which is exactly what
    hashing at the edge exists to prevent.

    ⚠ **Use a high-entropy secret** — ``openssl rand -hex 32`` or equivalent.
    The input space here is emails and user ids, which is small and guessable,
    so a memorable key defeats the entire purpose: anyone holding the database
    could dictionary-attack the column. A weak key is not a weaker pseudonym,
    it is none.

    Rotation seam: cut to a new key and new hashes carry a new scheme prefix
    while historical ones keep ``h1:``. The discontinuity is accepted and
    documented — the raw value was never stored, so nothing can be re-hashed."""

    resolve_session_id: ResolveSessionIdHook | None = None
    """Optional vendor-supplied session-id resolver, checked BEFORE the SPEC
    §3.4 ladder (rung 0) — a vendor who already has their own session/auth
    concept can hand Baton a real correlation key directly, bypassing MCP
    transport/meta entirely. The only mechanism that works on new-spec
    (SEP-2567) and true-stateless HTTP, where nothing MCP-native is
    observable by protocol design. A non-empty string return wins outright;
    ``None``/empty or a raised exception (logged, never propagated) falls
    through to the ladder unchanged. Return an opaque, non-PII id —
    passed through raw, not hashed; hashing/derivation is the vendor's
    responsibility if the raw value is sensitive. Sync or async."""

    tenant_id: str | None = None
    """Account identifier for the envelope's ``tenant_id`` (SPEC §11.4).

    **This is not ``vendor_id``, and conflating them is the bug this field
    exists to fix.** ``tenant_id`` names the ACCOUNT the collector
    authenticates; ``vendor_id`` names the SERVER whose surface is being
    captured. One account wraps many servers, so sending the account id in
    both slots collapses them: two servers in one workspace render as one,
    whose label flips to whichever deployed last, and a server ends up naming
    itself with its workspace's opaque id.

    Resolved explicit → ``BATON_TENANT_ID`` → ``vendor_id``. That last
    fallback exists for our own fixtures during the change, not for anyone's
    install — a wrap block states this value on its own line, because it is
    the diff a customer reviews in their pull request.
    """

    # Appended because it was added last, and nothing rides on that any more:
    # the class is ``kw_only`` as of 0.8.1, so there are no positional slots to
    # shift and field order is no longer public API. This comment used to say
    # the opposite, and cited a test that could not see the break 0.8.0 then
    # shipped — see the class docstring for what replaced the promise.

    dsn: str | None = None
    """The packed connection string from ``/account`` — one value carrying the
    ingest host, the workspace, the server and the key::

        install_baton(mcp, dsn="https://baton_pk_...@ingest.example.com/ten_.../echo-server")

    Supplying it fills ``vendor_id``, ``tenant_id`` and ``sink`` (an
    ``HttpSink`` at the DSN's origin, authenticated with its key), and
    ``vendor_display_name`` when that is not given. Grammar and rationale:
    ``baton._dsn``.

    Resolved explicit → ``BATON_DSN`` → unset. **Environment variables do not
    override it** — a DSN is the value stated in the vendor's source, and a
    stale ``BATON_*`` left over from an earlier install must not silently take
    a server's events somewhere else.

    Passing a DSN *and* an explicit ``vendor_id``, ``tenant_id`` or ``sink``
    raises: two sources for one value cannot be reconciled here without
    guessing, and a wrong guess routes a server's traffic under someone else's
    identity. ``vendor_display_name``, the scrubber, the injection modes and
    every identity option are unaffected — set them alongside a DSN freely."""


def build_config(config: VendorConfig | None, dsn: str | None) -> VendorConfig:
    """Resolve ``install_baton``'s two call shapes into one ``VendorConfig``.

    ``install_baton(mcp, dsn=...)`` is the whole wrap block a distributable
    server ships with; ``install_baton(mcp, VendorConfig(...))`` is what every
    vendor needing more than the defaults keeps using. A config carrying its
    own ``dsn`` field is the two combined, and is how you set a scrubber or an
    injection mode alongside a packed key.
    """
    if config is not None and dsn is not None:
        raise ValueError(
            "install_baton got both a VendorConfig and a dsn= argument. Put "
            "the dsn on the config — VendorConfig(dsn=...) — so there is one "
            "place holding it."
        )
    if config is None:
        if resolve_dsn(dsn) is None:
            raise ValueError(
                "install_baton needs either a VendorConfig or a dsn — the "
                "packed value from /account, which starts with https:// and "
                "can also arrive as BATON_DSN."
            )
        config = VendorConfig(dsn=dsn)
    return resolve_config(config)


def resolve_sink(config: VendorConfig) -> Sink:
    """``sink``: explicit → built from the ``dsn`` → ``StdoutSink``.

    The ``StdoutSink`` tail is the zero-config dev mode the SDK has always had:
    a vendor who wires nothing still sees their events as JSON Lines on stderr,
    which is the first thing that proves an install works at all.
    """
    return config.sink if config.sink is not None else StdoutSink()


def resolve_config(config: VendorConfig) -> VendorConfig:
    """Fill in whatever the DSN carries, returning a config nothing else has to
    know about. Called by both adapters BEFORE validation; a no-op when there
    is no DSN.

    **Everything a DSN supplies lands at the EXPLICIT tier, above the
    environment.** That is the whole point of the re-install shape it exists
    for: a server being re-onboarded has a stale ``.env`` from its last
    install sitting beside the new inline DSN, and if these values fell through
    to ``BATON_TENANT_ID`` and friends, the stale environment would quietly win
    and the events would arrive under the old identity. The DSN is the value
    the vendor's source file states; nothing ambient outranks it.

    Returns a NEW config rather than mutating the caller's — a vendor may hold
    a module-level ``VendorConfig`` and hand it to two servers, and an install
    that rewrites its argument would make the second one inherit the first's
    resolution.
    """
    dsn_string = select_dsn(
        config.dsn,
        {
            "vendor_id": bool(config.vendor_id),
            "tenant_id": config.tenant_id is not None,
            "sink": config.sink is not None,
        },
        "VendorConfig",
    )
    if dsn_string is None:
        return config

    dsn = parse_dsn(dsn_string)
    identity = replace(
        config,
        dsn=dsn_string,
        vendor_id=dsn.vendor_id,
        tenant_id=dsn.tenant_id,
        vendor_display_name=config.vendor_display_name or dsn.vendor_id,
    )

    # ⚠ **Validated BEFORE the sink is built, and the order is the point.**
    # ``HttpSink.__init__`` eagerly constructs an ``httpx.AsyncClient``, so a
    # config that fails validation for an unrelated reason — an emptied
    # consent_token, a bad user_id_mode — used to leave that client
    # unreachable and never closed, printing an unclosed-transport warning on
    # top of the real error. The callers validate again; it is pure, and a
    # second call costs nothing next to a resource that outlives its error.
    _validate_vendor_config(identity)

    return replace(
        identity,
        # Constructed here rather than lazily so a missing ``[http]`` extra
        # raises at install, where the vendor is watching — not at the first
        # tool call, in production, on somebody else's machine.
        sink=HttpSink(dsn.origin, api_key=dsn.key),
    )


def _validate_vendor_config(config: VendorConfig) -> None:
    if not config.vendor_id:
        raise ValueError(
            "VendorConfig needs a vendor_id — either directly, or via a dsn "
            "whose last path segment names the server (see /account)."
        )
    if not _VENDOR_ID_PATTERN.match(config.vendor_id):
        raise ValueError(
            f"vendor_id {config.vendor_id!r} must match "
            f"{_VENDOR_ID_PATTERN.pattern!r} — used as the default annotation "
            f"tool name prefix; dots and other separators are rejected by "
            f"Claude Desktop's tool-name validator."
        )
    if not config.vendor_display_name:
        raise ValueError(
            "VendorConfig needs a vendor_display_name — it names the vendor in "
            "the server instructions and the annotation tool, both of which the "
            "calling agent reads. A dsn supplies its server segment as the "
            "default; there is none to fall back on here."
        )
    if not config.consent_token:
        # Reachable only by passing ``""`` on purpose — the field defaults to a
        # real value — so this is a vendor emptying it, not one forgetting it.
        raise ValueError(
            "VendorConfig.consent_token was set to an empty string, and events "
            "without one MUST be rejected by the consumer per SPEC §2.3. Leave "
            "it unset to take the SDK's default."
        )
    if config.user_id_hmac_key is not None and not isinstance(
        config.user_id_hmac_key, bytes | bytearray | str
    ):
        raise ValueError(
            f"user_id_hmac_key must be bytes or str, got "
            f"{type(config.user_id_hmac_key).__name__} — it keys an HMAC, and "
            f"a wrong type fails at the FIRST AUTHENTICATED CALL rather than "
            f"here, which is a deployment a vendor cannot reach in local "
            f"testing."
        )
    if config.user_id_mode not in USER_ID_MODES:
        raise ValueError(
            f"user_id_mode {config.user_id_mode!r} must be one of "
            f"{sorted(USER_ID_MODES)} — 'hashed' emits an HMAC pseudonym, "
            f"'raw' emits the end user's identity verbatim to the collector."
        )
    if config.intent_param_mode not in _INTENT_PARAM_MODES:
        raise ValueError(
            f"VendorConfig.intent_param_mode {config.intent_param_mode!r} must be "
            f"one of {sorted(_INTENT_PARAM_MODES)}."
        )
    if config.proactive_mode not in _PROACTIVE_MODES:
        raise ValueError(
            f"VendorConfig.proactive_mode {config.proactive_mode!r} must be "
            f"one of {sorted(_PROACTIVE_MODES)}."
        )
    if config.intent_param_mode == "off" and config.proactive_mode == "off":
        raise ValueError(
            "VendorConfig has intent_param_mode='off' and proactive_mode='off' — "
            "nothing would capture what the user is trying to do. Set one of "
            "them: intent_param_mode='optional' (injected params, the default "
            "channel) or proactive_mode='on' (agent-filed pre-call annotations, "
            "for vendors who won't accept tool-schema mutation)."
        )
