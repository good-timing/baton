"""Shared VendorConfig used by both adapter ``install_baton`` functions."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from baton._dsn import VENDOR_ID_PATTERN as _VENDOR_ID_PATTERN
from baton._dsn import parse_dsn, resolve_dsn, select_dsn
from baton.events import DEFAULT_CONSENT_TOKEN
from baton.integrations.identity_adapter import (
    PRINCIPAL_ID_MODE_HASHED,
    PRINCIPAL_ID_MODES,
    ResolvePrincipalHook,
)
from baton.sinks import HttpSink, Sink, StdoutSink

# ``_VENDOR_ID_PATTERN`` is imported, not defined here, because a DSN's server
# segment IS a vendor_id and both rules have to be one object. Restating the
# ceiling as a second regex is how the two drift; this repo has already costed
# that number wrong twice.

logger = logging.getLogger(__name__)

# Per-tool intent-param injection modes (mirrors baton-proxy's BATON_INTENT_PARAM).
_INTENT_PARAM_MODES: frozenset[str] = frozenset({"optional", "required", "off"})
_PROACTIVE_MODES: frozenset[str] = frozenset({"on", "off"})


class CaseInsensitiveHeaders(dict[str, str]):
    """The standalone adapter's header dict, with case-folded lookups.

    Exists so ``SessionResolutionContext.headers`` resolves the same key on both
    adapters. The official ``mcp`` adapter reads Starlette ``Headers``, which is
    already case-insensitive; the standalone ``fastmcp`` adapter reads
    ``get_http_headers()``, a plain ``dict`` whose keys ASGI has already
    lowercased. Both satisfy the declared ``Mapping[str, str]``, so the
    divergence is invisible to mypy — and a vendor writing the canonical
    spelling (``ctx.headers["X-Forwarded-User"]``) hit **4/4 on official and
    0/4 on standalone**, measured across all six supported resolves.

    ⚠ **It subclasses ``dict`` rather than wrapping one, and that is the whole
    point of the shape.** Standalone hooks have received a real ``dict`` since
    the field existed, so a ``Mapping`` wrapper would take away
    ``isinstance(ctx.headers, dict)``, ``.copy()``, ``json.dumps(ctx.headers)``
    and ``|`` — and since ``resolve_principal_via_hook`` catches bare
    ``Exception``, a hook that merely logged its headers before reading a key
    would have started dropping ``principal`` silently. That is the identical
    fail-open this class exists to close, re-created pointing the other way. As
    a ``dict`` subclass every behaviour a standalone hook had is preserved and
    three read paths additionally fold case; methods not listed below
    (``pop``, ``setdefault``) still take the stored lowercase key, exactly as
    they did before.

    ⚠ **The direction is one-way: standalone comes UP, official is never taken
    DOWN.** Official vendors may already rely on case-insensitive lookup, so
    converting them to a plain ``dict`` breaks hooks that work today. Wrapping
    BOTH sides in this class was considered and rejected for the same reason in
    miniature: it would keep case-insensitivity but remove Starlette's
    ``getlist()`` and its ``__eq__``.

    ⚠ **So what is normalized is LOOKUP, not type, and the residual divergence
    is real** — ``getlist()`` exists only on official, and
    ``ctx.headers == {"x-a": "1"}`` is ``True`` here and ``False`` against
    Starlette ``Headers``. A hook doing either still needs to know its adapter.

    ⚠ **Duplicate header lines are NOT reconciled and the two adapters
    disagree** — measured, not assumed. Given ``x-forwarded-user: alice`` then
    ``x-forwarded-user: bob`` (an ordinary proxy-chain append), Starlette
    returns the FIRST (``alice``) and ``get_http_headers`` the LAST (``bob``),
    because it builds its dict with ``headers[name] = value`` over every line.
    That collapse happens upstream, before this class sees anything, so nothing
    here can repair it — the first value is already gone. Recorded rather than
    papered over; an earlier draft of this docstring claimed upstream joined
    duplicates, which was written from assumption and is false.
    """

    def __init__(self, raw: Mapping[str, str]) -> None:
        super().__init__({key.lower(): value for key, value in raw.items()})

    @staticmethod
    def _fold(key: Any) -> Any:
        """Lowercase a string key, pass anything else through untouched.

        The pass-through is the same rule as the class itself: a plain ``dict``
        answers ``.get(None)`` with ``None`` and ``d[None]`` with ``KeyError``,
        and calling ``.lower()`` unconditionally would turn both into
        ``AttributeError`` — a new exception from a hook that used to work,
        swallowed by the same fail-open guard, dropping ``principal``. Preserving
        dict behaviour means preserving it for the odd key too.
        """
        return key.lower() if isinstance(key, str) else key

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(self._fold(key))

    def __contains__(self, key: object) -> bool:
        return super().__contains__(self._fold(key))

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(self._fold(key), default)

    # ⚠ **The write path folds too, and leaving it out was a real defect.**
    # Folding reads alone broke the class's own invariant — every stored key is
    # lowercase — so ``h["X-B"] = "2"`` stored ``X-B`` verbatim and then
    # ``h["X-B"]`` raised ``KeyError`` for a key ``list(h)`` plainly showed.
    # That is strictly worse than the plain ``dict`` this replaced, where
    # set-then-read simply worked, and it lands in the same silent place: a
    # hook that normalizes before reading (``setdefault`` a fallback, then read
    # it back) raises, ``resolve_principal_via_hook`` swallows it, and
    # ``principal`` is absent on every event. The same fail-open this class
    # exists to close, re-created on the other half of the mapping protocol.

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(self._fold(key), value)

    def __delitem__(self, key: str) -> None:
        super().__delitem__(self._fold(key))

    def pop(self, key: str, *default: Any) -> Any:
        return super().pop(self._fold(key), *default)

    def setdefault(self, key: str, default: Any = None) -> Any:
        return super().setdefault(self._fold(key), default)

    def update(self, *args: Any, **kwargs: str) -> None:
        # Normalized through one path rather than per-overload: ``dict.update``
        # accepts a mapping, an iterable of pairs, or keywords, and folding
        # only the branch that came to mind is how the read path ended up
        # half-done.
        merged: dict[str, str] = {}
        for source in args:
            items = source.items() if hasattr(source, "keys") else source
            for key, value in items:
                merged[self._fold(key)] = value
        for key, value in kwargs.items():
            merged[self._fold(key)] = value
        super().update(merged)


@dataclass(frozen=True)
class SessionResolutionContext:
    """Normalized input to ``VendorConfig.resolve_principal``.

    Deliberately does not carry the raw SDK ``Context`` object — the
    official ``mcp`` and standalone ``fastmcp`` libraries expose different,
    adapter-specific ``Context`` types. This shape is what's already
    extracted for both adapters (headers, meta), so one hook works
    unmodified regardless of which adapter a vendor is on.

    ⚠ **That sentence was FALSE for ``headers`` until 2026-09-13** (register
    A8). Declaring ``Mapping[str, str]`` normalized the type and not the
    behaviour: official delivered a case-insensitive Starlette ``Headers``,
    standalone a plain lowercased ``dict``, and an exact-case lookup therefore
    worked on one adapter and raised ``KeyError`` on the other — silently, since
    this path is fail-open throughout.

    **The guarantee is now enforced HERE, in ``__post_init__``, rather than in
    each adapter's extractor.** Of the four fields, ``headers`` is the only one
    declared abstractly, and it is the only one that diverged — the other three
    are concrete types mypy forces every adapter to normalize before it can
    construct this object. The abstract annotation WAS the hole, so a fix that
    lived in one adapter's extractor would leave the class promising something
    only a convention upheld: a fifth construction site, a third extractor or
    the planned ``claude_code`` adapter would re-open A8 with no type error and
    no failing test, which is exactly how it shipped the first time. It also
    covers the case an adapter fix cannot reach — **a vendor unit-testing their
    own hook constructs this object by hand**, and a hand-written
    ``{"X-Forwarded-User": ...}`` would otherwise behave unlike production in
    whichever direction they happened to spell it.

    ⚠ **The symptom differs by deployment and the worse one is not the obvious
    one.** Where no token exists (stdio, or HTTP with no OAuth) the miss yields
    no ``principal`` on any event. But where a token DOES exist — HTTP +
    OAuth, the shape ``X-Forwarded-User`` actually lives in — the hook is rung 0
    and a raise falls through to rung 1, so events carried the token's ``h1:``
    pseudonym instead of the hook's ``v1:`` one. Same person, two different
    pseudonyms depending on which adapter the vendor shipped: an actor split,
    not an absence. A plain dict is folded below, in ``__post_init__``; see
    ``CaseInsensitiveHeaders`` for the direction rule.

    ⚠ **The name is a fossil.** This was built for
    ``VendorConfig.resolve_session_id``, which was REMOVED 2026-09-12 (SPEC
    §3.4 rung 0). ``resolve_principal`` had already adopted the same four fields,
    so the shape outlived the hook it was named for. Kept under its released
    name rather than renamed: it has been public since 0.7.x and a rename is
    a second breaking change for a cosmetic gain.
    """

    headers: Mapping[str, str] | None
    meta: dict[str, Any] | None
    tool_name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        """Fold a plain ``dict`` of headers; pass anything else through.

        ⚠ **Fold by DEFAULT; pass through only a multi-value container.** The
        rule is stated this way round deliberately. An earlier version gated on
        ``isinstance(headers, dict)``, which folded the two shapes the adapters
        ship today and silently missed every other mapping — a
        ``MappingProxyType``, a ``UserDict``, a future ``get_http_headers()``
        return type. That is narrower than the guarantee the paragraph above
        claims, and it holed the very case that argued for putting the fold
        here: a vendor hand-building this object in their own unit tests.

        ``getlist`` is the pass-through test because a mapping exposing it
        holds MORE than a flat mapping can — repeated header lines kept apart
        — so folding it would lose data rather than merely change lookups.
        Every such container in practice (Starlette's ``Headers``, which is
        what the official adapter delivers, and werkzeug's) is already
        case-insensitive, so the exemption costs nothing and keeps
        ``getlist()`` working for official vendors who use it. That is the
        one-way rule ``CaseInsensitiveHeaders`` documents, made structural:
        there is no arm here that can take official down to a flat dict.
        """
        if self.headers is None or isinstance(self.headers, CaseInsensitiveHeaders):
            return
        if hasattr(self.headers, "getlist"):
            return
        object.__setattr__(self, "headers", CaseInsensitiveHeaders(self.headers))


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


def _resolve_principal_id_hmac_key(explicit: bytes | str | None, *, mode: str) -> bytes | None:
    """``principal_id_hmac_key``: explicit → ``BATON_PRINCIPAL_ID_HMAC_KEY`` → ``None``.

    In hashed mode with neither set, a leftover ``BATON_USER_ID_HMAC_KEY`` (the
    pre-0.8.6 name, which is NOT read) logs a WARNING: identity fails open, so
    nothing else would say it stopped. ``mode`` is required so no caller can
    skip that distinction.

    ``None`` is a supported state, not an error: it means hashed-mode identity
    is off and events emit without ``principal``.
    """
    if explicit is not None:
        # A ``str`` is encoded rather than refused, because the env path has
        # always taken one and a vendor moving a working secret from
        # ``BATON_PRINCIPAL_ID_HMAC_KEY`` into the field would otherwise hit
        # ``hmac.new``'s "expected bytes" TypeError — and only in the
        # deployment shape this field exists for (HTTP + OAuth), so never in
        # their local testing. Same secret, same bytes, either way.
        return explicit.encode("utf-8") if isinstance(explicit, str) else explicit
    from_env = os.environ.get("BATON_PRINCIPAL_ID_HMAC_KEY")
    if (
        mode == PRINCIPAL_ID_MODE_HASHED
        and not from_env
        and os.environ.get("BATON_USER_ID_HMAC_KEY")
    ):
        logger.warning(
            "baton: BATON_USER_ID_HMAC_KEY is set, but it was renamed to "
            "BATON_PRINCIPAL_ID_HMAC_KEY in 0.8.6 and is no longer read, so hashed "
            "principal_id is OFF. Set BATON_PRINCIPAL_ID_HMAC_KEY to turn it back on."
        )
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
    inserted before the then-existing ``resolve_session_id``, so a 0.7.2-shaped call put the
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
    ``"example-vendor"``). Used for the annotation tool name only as a
    FALLBACK — the default is derived from your MCP server's own name, and this
    is what it falls back to (``{vendor_id}_annotate``) when the server carries
    no name of its own; must match the cross-runtime tool-name pattern.

    Required unless a ``dsn`` supplies it — the DSN's last path segment is
    this value, and it is what the key is BOUND to."""

    vendor_display_name: str = ""
    """Human-readable vendor name used in server instructions, annotation
    tool description, and any LLM-facing strings. Whitelabel obligation
    (SPEC §5.4): no Baton-branded strings reach the calling agent.

    Defaults, when a ``dsn`` is given and this is not, to your MCP server's own
    name verbatim (``FastMCP("Toybox Pantry")`` reads "Toybox Pantry"), and to
    the DSN's server segment when the server carries no name of its own or its
    name would not fit the server instructions. Verbatim rather than
    prettified: this string reaches the calling agent, so inventing a
    capitalisation the vendor never chose would put a fabricated name in front
    of their users. The server-name half is settled at install, where the
    server is in hand (``resolve_annotation_names``); ``resolve_config`` on its
    own still returns the DSN segment."""

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
    """Optional override for the annotation tool name, and the only thing that
    pins it.

    The default is derived from your MCP server's own name — a server called
    ``"Acme Knowledge Base"`` registers ``acme-knowledge-base_annotate`` — and
    falls back to ``{vendor_id}_annotate`` when the server carries a name the
    LIBRARY invented rather than one you chose. Set this to keep a name you
    have written into documentation, a prompt or a script: it wins over both."""

    scrubber: Callable[[Any], Any] | None = None
    """PII scrubber per SPEC §7. Default (None) uses ``baton.scrub.Scrubber``
    — recursive walker with email/Bearer/sk-*/AKIA*/JWT/CC-Luhn/phone
    patterns + field-name overrides on by default. Pass
    ``baton.scrub.identity_scrub`` to opt out, or supply your own."""

    intent_param_mode: str = "required"
    """Per-tool intent-param injection (mirrors baton-extmcp's vendor-neutral
    naming). Every mode but ``"off"`` injects ``user_goal``, ``expected_result``
    and ``overall_task`` string params into each wrapped tool's input schema
    and strips all three before the vendor handler runs, so the tool never
    sees them. This is what captures intent on runtimes that drop
    ``instructions`` (notably Claude Desktop), where the annotation tool alone
    yields nothing.

    ``"required"`` (default) also lists ``user_goal`` in each tool's advertised
    ``required`` and leads its description with REQUIRED. That is an
    advertisement and nothing more: nothing Baton adds rejects a call that
    omits it, the vendor's handler runs, and the event carries no
    ``call_intent``. Measured 2026-09-15 through a real client session on both
    adapters, on every mcp and fastmcp version CI's matrix pins.
    ``"optional"`` injects the same params without the ``required`` entry, and
    was the default until 2026-09-15. ``"off"`` disables injection.
    ``expected_result`` and ``overall_task`` stay optional in every mode, and a
    tool that already declares one of these names keeps its own."""

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

    principal_id_mode: str = "hashed"
    """How a resolved principal reaches the wire (SPEC §11.4 ``principal.form``).
    ``"hashed"`` (default) emits ``<scheme>:<hex>`` — an HMAC computed in this
    process, so the collector only ever sees the pseudonym. ``"raw"`` emits the
    principal verbatim.

    **``"raw"`` puts real identities in the collector's database.** It
    is the right choice for a vendor instrumenting a server whose users are
    themselves, or one with no residency obligation who would rather read a
    name than a hash — and the wrong choice by default, which is why it is not
    the default. On a multi-tenant vendor server the principals are the
    VENDOR's customers, and shipping their identities to a third party is a
    decision only that vendor can make.

    Hashed mode needs ``principal_id_hmac_key``; raw mode needs nothing. The two are
    distinguishable on the wire without a second field, because a hashed value
    always carries a registered scheme prefix (``h1:`` attested, ``v1:``
    asserted) and a consumer treats anything else as a raw identity."""

    principal_id_hmac_key: bytes | str | None = field(default=None, repr=False)
    """Secret keying the ``principal.id`` HMAC in ``"hashed"`` mode.

    ⚠ **``repr=False`` — PRE-EXISTING, and not part of the DSN lane that
    brought the other two.** It is the same defect in the same ``repr`` for the
    same reason, found while fixing them: a field whose own docstring says the
    vendor holds it and Baton never sees it has no business printing itself.

    Resolved explicit → ``BATON_PRINCIPAL_ID_HMAC_KEY`` → ``None``. Unset means
    hashed identity is fail-open-skipped: ``principal`` is dropped, events still
    emit, and it is logged once. ``principal`` is additive analytics — never a
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

    resolve_principal: ResolvePrincipalHook | None = None
    """Optional vendor-supplied identity resolver, checked BEFORE the verified
    access token and winning outright when both resolve (SPEC §11.4).

    **This is the only way a stdio vendor's principal can reach Baton.** The
    token path reads a contextvar set by MCP's bearer-auth ASGI middleware, and
    stdio has no ASGI — so ``principal`` is HTTP-only without this hook, on every
    supported version. A vendor already authenticating stdio users out of band
    knows exactly who the user is and previously had no way to say so.

    Takes a ``SessionResolutionContext`` — headers, meta, tool name and
    arguments — and returns ``Principal | None``; ``None``, a wrong type, or a raised exception
    (logged, never propagated) falls through to the token path unchanged.
    Sync or async. Import the return type as ``from baton import Principal``.

    ⚠ **A hook principal is ASSERTED, not attested.** The token path carries an
    identity an IdP verified; this one carries whatever the vendor says, and the
    SDK cannot check it. So it hashes under its own scheme tag — ``v1:`` rather
    than ``h1:`` — and a consumer can tell the two apart on the wire. It is
    checked ABOVE the token deliberately: a gateway's token frequently names a
    service account rather than the end user, and this hook exists only where a
    vendor opted in, which makes it the more specific claim even though it is
    the less verified one.

    ⚠ **In ``principal_id_mode="raw"`` the distinction is not on the wire**, because
    raw mode emits the principal verbatim and untagged from both paths. Raw
    mode forfeits provenance the same way it forfeits pseudonymity; if you need
    to tell asserted from attested downstream, use hashed mode.

    ⚠ **It is not ``default_agent_runtime`` returning.** That was a static
    value set once at install, asserting over whatever a client declared per
    connection, and could only be right in a single-client deployment. This is
    a callable invoked per request with that request's own context, and a
    different failure mode."""

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

    dsn: str | None = field(default=None, repr=False)
    """The packed connection string from ``/account`` — one value carrying the
    ingest host, the workspace, the server and the key::

        install_baton(mcp, dsn="https://baton_pk_...@ingest.example.com/ten_.../echo-server")

    Supplying it fills ``vendor_id``, ``tenant_id`` and ``sink`` (an
    ``HttpSink`` at the DSN's origin, authenticated with its key), and lets
    ``vendor_display_name`` default when that is not given (see that field).
    Grammar and rationale: ``baton._dsn``.

    ⚠ **``repr=False``, because this string contains the bearer.** Measured:
    ``repr(config)`` printed the whole DSN, key included, and this config is
    RETAINED after resolution (``resolve_config`` copies the packed string onto
    the config it returns) — so any traceback rendering locals, any structured
    log line taking a config, and every plain ``print`` of one wrote a
    publishable key out. Its Python twin is ``Dsn.key``, fixed the same way.

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
    # ⚠ ``and dsn``, not ``and dsn is not None`` — the falsy-means-unset rule
    # has to hold at EVERY door or it is not a rule. This check was missed when
    # `select_dsn` was fixed, so the exact shape that fix cites,
    # ``dsn=os.environ.get("MY_DSN", "")``, still died one door over. Found by
    # review, reproduced before fixing.
    if config is not None and dsn:
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
            # ``bool``, matching ``vendor_id`` directly above: an empty
            # ``tenant_id`` is a value nobody set, so it must not be reported as
            # a conflict against a DSN. The line above was already ``bool`` and
            # this one was not — the same rule written two ways, one line apart.
            "tenant_id": bool(config.tenant_id),
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
        # The PRE-INSTALL default, and only that: install replaces it with the
        # server's own name (``resolve_annotation_names``). Kept here because
        # the config this returns must validate, and validation refuses an
        # empty display name.
        vendor_display_name=config.vendor_display_name or dsn.vendor_id,
    )

    # ⚠ **Validated BEFORE the sink is built, and the order is the point.**
    # ``HttpSink.__init__`` eagerly constructs an ``httpx.AsyncClient``, so a
    # config that fails validation for an unrelated reason — an emptied
    # consent_token, a bad principal_id_mode — used to leave that client
    # unreachable and never closed, printing an unclosed-transport warning on
    # top of the real error. The callers validate again; it is pure, and a
    # second call costs nothing next to a resource that outlives its error.
    _validate_vendor_config(identity)

    return replace(
        identity,
        # Constructed here rather than lazily so a sink that cannot be built
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
    if config.principal_id_hmac_key is not None and not isinstance(
        config.principal_id_hmac_key, bytes | bytearray | str
    ):
        raise ValueError(
            f"principal_id_hmac_key must be bytes or str, got "
            f"{type(config.principal_id_hmac_key).__name__} — it keys an HMAC, and "
            f"a wrong type fails at the FIRST AUTHENTICATED CALL rather than "
            f"here, which is a deployment a vendor cannot reach in local "
            f"testing."
        )
    if config.resolve_principal is not None and not callable(config.resolve_principal):
        raise ValueError(
            f"VendorConfig.resolve_principal must be callable, got "
            f"{type(config.resolve_principal).__name__}. Unvalidated it would fail "
            f"inside the hook's own fail-open guard — logged, identity "
            f"silently absent for the life of the process — and that guard is "
            f"there for a vendor's resolver raising, not for the field holding "
            f"the wrong thing."
        )
    if config.principal_id_mode not in PRINCIPAL_ID_MODES:
        raise ValueError(
            f"principal_id_mode {config.principal_id_mode!r} must be one of "
            f"{sorted(PRINCIPAL_ID_MODES)} — 'hashed' emits an HMAC pseudonym, "
            f"'raw' emits the principal verbatim to the collector."
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
            "them: intent_param_mode='required' (injected params, the default "
            "channel) or proactive_mode='on' (agent-filed pre-call annotations, "
            "for vendors who won't accept tool-schema mutation)."
        )
