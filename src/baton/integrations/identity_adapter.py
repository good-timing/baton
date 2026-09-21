"""End-user identity off the MCP auth seam — resolve the principal, hash it
at the edge.

Shared by BOTH adapters, beside ``runtime_adapter.py`` and for the same reason:
the last capture signal that lived under one adapter's package was the one the
other adapter never called, and it shipped ``unknown`` on every event for two
releases before anybody noticed.

**This is the attested half of identity.** ``agent_runtime`` is what a client
says it is — self-reported, never verified, and a client may call itself
anything. A principal resolved here from a bearer token the VENDOR's own
verifier already validated is the one identity claim on the envelope that
something checked — and it is marked ``principal.source == "attested"`` so a
consumer can tell. ⚠ **The field as a whole is NOT attested identity.** This
module resolves both provenances, and the other one — a vendor's
``resolve_principal`` hook — is checked by nothing; SPEC §11.4 forbids
presenting it as verified. Keep all of it apart from ``agent_runtime``: they
answer different questions and they are trustworthy to different degrees.

**Transport reality: this is HTTP-only, on every supported version.** MCP auth
is ASGI middleware (``mcp.server.auth.middleware.bearer_auth`` operates on a
Starlette ``Scope``), and ``get_access_token()`` reads a contextvar that
middleware sets. On stdio nothing sets it, so there is no token, no principal
and no attested ``principal`` — not a failure, just the shape of the transport. A vendor
on stdio who wants identity needs a different carrier entirely.

⚠ **``claims`` does not exist on ``mcp < 1.27``.** ``AccessToken`` gained
``claims`` and ``subject`` somewhere in (1.25, 1.27]; on 1.20 and 1.25 — two of
the four legs ``mcp-matrix`` runs — the model carries only
``token``/``client_id``/``scopes``/``expires_at``/``resource``. Pydantic's
default ``extra`` is *ignore*, so a vendor verifier that passes ``claims=…`` on
that band has it **silently dropped** (measured, not read). Hence ``getattr``
rather than attribute access: on the old band this resolves to ``None`` and the
field is simply absent, while a vendor who declares ``claims`` on an
``AccessToken`` SUBCLASS is read correctly on every version. Raising the floor
was considered and rejected — 1.20/1.25 are green, and they lack only an
optional field on a feature that needs HTTP plus OAuth to do anything at all.

⚠ **Never ``client_id``.** It names the OAuth APPLICATION, not the person —
measured identical (``acme-desktop-app``) for two different users across every
version tested. Keying identity on it merges every user of one app, which is
the exact merge this field exists to resolve. It looks user-shaped in a naive
test only because ``JWTVerifier`` falls back ``client_id ?? azp ?? sub``.

``subject`` is not read either, and that is deliberate rather than an omission:
``claims["sub"]`` was populated on every combination measured (2026-09-07),
including everywhere ``subject`` was, while ``subject`` is ``None`` for every
user across the whole fastmcp 2.x/3.x band and is *dropped* by fastmcp 3.4.2's
own ``AccessToken`` rebuild. One read of the field that always works beats two
reads where the shortcut is the buggier path.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from baton.events import PrincipalWire
from baton.identity import Principal, hash_principal_id
from baton.integrations._hooks import run_vendor_hook

if TYPE_CHECKING:
    # Type-only, and it has to be: ``_config`` imports ``ResolvePrincipalHook`` and
    # ``PRINCIPAL_ID_MODES`` from this module at runtime, so a runtime import back
    # would be a cycle. The CALLER builds the context and passes it in, which
    # is also why ``resolve_call_principal`` takes one rather than making one.
    from baton.integrations._config import SessionResolutionContext

#: Emit the HMAC of the principal. The console sees ``h1:<hex>`` and never the
#: raw identity — the residency contract (the console DB is metadata-only) and
#: the default.
PRINCIPAL_ID_MODE_HASHED = "hashed"

#: Emit the principal VERBATIM. A deliberate opt-in that puts real identities
#: on the wire and into the console database. Correct for a vendor
#: dogfooding their own server, or one with no residency obligation who wants
#: to read a name instead of a hash; wrong by default, which is why it is not
#: the default.
PRINCIPAL_ID_MODE_RAW = "raw"

#: The mode→form mapping, and the source of truth for which modes exist.
#:
#: Written out rather than deriving ``form`` from ``mode`` at the call site,
#: so a third mode cannot silently become a third ``form``. Deriving
#: ``PRINCIPAL_ID_MODES`` from it closes the other direction too: a mode
#: cannot be registered without someone choosing what it means for a
#: consumer's classification.
#:
#: ⚠ The two vocabularies overlapping is a coincidence of spelling, not a
#: derivation. SPEC §11.4 requires ``source`` and ``form`` to stay independent
#: ("every combination occurs"); ``form`` is the mode's OUTCOME and ``source``
#: is which rung resolved the principal.
_FORM_BY_MODE = {
    PRINCIPAL_ID_MODE_HASHED: "hashed",
    PRINCIPAL_ID_MODE_RAW: "raw",
}

PRINCIPAL_ID_MODES = frozenset(_FORM_BY_MODE)

#: ``principal.source`` — a verified token's ``sub``. An identity provider
#: checked this. HTTP-only, on every supported version.
PRINCIPAL_SOURCE_ATTESTED = "attested"

#: ``principal.source`` — a vendor's own per-request resolver. The vendor
#: states who this is and nothing in the protocol checked the claim. The only
#: identity mechanism that exists on stdio, and NOT a degraded ``attested``.
PRINCIPAL_SOURCE_ASSERTED = "asserted"

#: Cap on a RAW principal. Same posture and same number as the declared
#: ``agent_runtime`` tiers: it is external text copied onto every event of the
#: call, so it gets a bound. Hashed values are fixed-width by construction and
#: are not capped — truncating a digest would destroy the join.
RAW_PRINCIPAL_ID_MAX_LEN = 128


#: A vendor's per-request identity resolver. Takes the adapter-neutral
#: ``SessionResolutionContext`` — headers, meta, tool name and arguments — so
#: it is not coupled to either library's ``Context`` type. The shape and its
#: name are inherited from ``resolve_session_id``, which shared it until that
#: hook was removed 2026-09-12; this is now its only caller.
ResolvePrincipalHook = Callable[
    ["SessionResolutionContext"], "Awaitable[Principal | None] | Principal | None"
]


async def resolve_principal_via_hook(
    hook: ResolvePrincipalHook,
    context: SessionResolutionContext,
    *,
    logger: logging.Logger,
) -> Principal | None:
    """Call a vendor's ``resolve_principal`` hook and normalize its result.

    Never raises. An exception is logged and treated as a miss, so identity
    falls through to the verified-token path exactly as if no hook were
    configured — ``principal`` is additive analytics and a vendor's own bug in
    their resolver may not fail their tool call (SPEC §11.2 fail-open).

    Accepts sync or async hooks, mirroring ``VendorConfig.scrubber``.

    ⚠ **The ``isinstance`` is load-bearing, not defensive typing.** A hook
    returning a dict, a bare string, or a namedtuple with the right field names
    is the shape a vendor reaches for first — AgentCat's ``identify()``, the
    prior art this hook is modelled on, returns a dict — and duck-typing it
    would put an unvalidated value on the identity path, where the very next
    thing that happens is an HMAC over ``principal.principal_id``. A wrong return is
    a miss, logged, never a partially-built ``Principal``.
    """
    try:
        result = await run_vendor_hook(hook, context, hook_name="resolve_principal", logger=logger)
    except Exception:
        logger.warning("baton: resolve_principal hook raised; falling through", exc_info=True)
        return None
    if result is None:
        return None
    if not isinstance(result, Principal):
        logger.warning(
            "baton: resolve_principal hook returned %s, not a baton.Principal — "
            "ignoring it and falling through to the verified token. Return "
            "Principal(principal_id=...) or None.",
            type(result).__name__,
        )
        return None
    if not isinstance(result.principal_id, str) or not result.principal_id.strip():
        # An empty principal_id would hash to a real, stable digest naming nobody,
        # merging every such caller into one actor — the exact collapse this
        # field exists to undo. A miss, not a value.
        #
        # ⚠ ``.strip()``, not truthiness. ``hash_principal_id`` canonicalizes with
        # NFC → strip → lower, so " " and "\t\n" hash IDENTICALLY — measured
        # ``h1:14fa5f91…`` for both. A blank header value or a padded CHAR(n)
        # column is the reachable shape, and truthiness waves it straight
        # through into exactly the merge the sentence above claims to stop.
        logger.warning(
            "baton: resolve_principal hook returned a Principal with an empty or "
            "non-string principal_id — ignoring it and falling through."
        )
        return None
    # ``issuer`` gets the SAME coercion the attested path applies to
    # ``claims["iss"]``, and for two reasons that are both bugs without it.
    #
    # (1) It is folded into the HMAC message only when it is not ``None``, so
    #     ``issuer=""`` and ``issuer=None`` produce DIFFERENT digests for one
    #     person. A vendor writing the natural
    #     ``Principal(principal_id=sub, issuer=claims.get("iss", ""))`` would get a
    #     stable-but-wrong pseudonym, and switching to ``None`` later would
    #     silently rename every one of their users — the actor split this
    #     field exists to prevent, introduced by the field itself.
    # (2) A non-string issuer — a UUID object, an int tenant id — reaches
    #     ``unicodedata.normalize`` and raises ``TypeError``. That is caught
    #     downstream, but the cost is the whole event's ``principal``, including
    #     the attested one the token could still have produced. Coercing here
    #     means a junk issuer costs the issuer, not the identity.
    if not isinstance(result.issuer, str) or not result.issuer:
        result = replace(result, issuer=None)
    return result


def principal_from_access_token(token: Any) -> Principal | None:
    """Read ``(sub, iss)`` off a verified access token.

    ``token`` is whatever the adapter's ``get_access_token()`` returned —
    ``None`` when the request is unauthenticated, which is most requests and
    every stdio one. Returns ``None`` whenever no usable subject is present;
    never raises, because an identity read must not be able to fail a tool call
    (SPEC §11.2 fail-open).

    The token has ALREADY been validated by the vendor's own ``TokenVerifier``
    — the SDK does not verify signatures and must never be read as if it had.
    What arrives here is the verifier's own parsed output.
    """
    if token is None:
        return None
    try:
        # getattr, not attribute access: absent on mcp < 1.27 entirely, and
        # present-but-None whenever the vendor's verifier did not populate it.
        claims = getattr(token, "claims", None)
        if not isinstance(claims, dict):
            return None
        sub = claims.get("sub")
        # ``.strip()`` for the same reason as the hook path's guard: the
        # canonicalizer strips, so a whitespace-only subject is a phantom
        # actor every such caller merges into. Far less reachable here — a
        # verifier would have to mint one — but the two paths feed one hash
        # and a guard that differs between them is a guard waiting to be
        # copied wrong.
        if not isinstance(sub, str) or not sub.strip():
            return None
        issuer = claims.get("iss")
        if not isinstance(issuer, str) or not issuer:
            issuer = None
        return Principal(principal_id=sub, issuer=issuer)
    except Exception:
        # Broad on purpose, same reason ``runtime_adapter`` is: this reads
        # attributes off an object a VENDOR's verifier constructed, and an
        # identity read may not be able to fail a tool call. An enumerated
        # tuple in the sibling module missed fastmcp's ``RuntimeError`` and
        # shipped exactly that escape.
        return None


def resolve_attested_principal(
    token: Any,
    *,
    mode: str,
    tenant_id: str,
    hmac_key: bytes | None,
    logger: logging.Logger,
    warned: set[str],
) -> PrincipalWire | None:
    """Resolve a verified token into the envelope's ``principal``, or ``None``.

    **The ATTESTED path, and only that one.** It reads the token and hands the
    principal to ``_finish_principal`` — the edge-hash chokepoint, where the
    raw value becomes its final wire form and no caller downstream ever holds
    the raw value. That mirrors baton-proxy's ``Emitter._enqueue`` discipline,
    where the same rule is what keeps raw identity from reaching a
    console-bound sink by some path nobody audited.

    Callers on an emit path want ``resolve_call_principal``, which checks a
    vendor's ``resolve_principal`` hook first and falls through to this. This
    function remains the whole of identity for a deployment with no hook
    configured, which is every deployment today.

    ``warned`` is a caller-owned set used to log the missing-key and
    unknown-mode cases exactly once per install rather than once per tool call.

    Fail-open throughout — every branch that cannot produce a value returns
    ``None`` and the event ships without the field:

    - no auth on the request (or stdio, where there is none) → ``None``
    - ``mcp < 1.27`` with no ``claims`` on the token → ``None``
    - ``hashed`` with no HMAC key configured → ``None``, warned once
    - a ``mode`` this module does not recognise → ``None``, warned once. It
      has no truthful ``form``, and ``form`` is what a consumer classifies on
    - ``raw`` → the subject verbatim, no key needed

    ``principal`` is additive analytics. It is never a consent or authorization
    gate, so nothing here may raise, and nothing here may stop an event.
    """
    return _finish_principal(
        principal_from_access_token(token),
        mode=mode,
        tenant_id=tenant_id,
        hmac_key=hmac_key,
        source=PRINCIPAL_SOURCE_ATTESTED,
        logger=logger,
        warned=warned,
    )


def _finish_principal(
    principal: Principal | None,
    *,
    mode: str,
    tenant_id: str,
    hmac_key: bytes | None,
    source: str,
    logger: logging.Logger,
    warned: set[str],
) -> PrincipalWire | None:
    """Turn a resolved principal into the finished wire object, or ``None``.

    **The edge-hash chokepoint, and the single copy of it.** Both provenances
    end here — the attested token read and the asserted ``resolve_principal`` hook —
    so the cap, the raw-mode rules, the missing-key warning and the hashing
    failure mode are the same for both by construction rather than by two
    implementations agreeing. They differ in exactly one argument, ``source``,
    which is the whole point: the member a consumer reads to know which kind of
    claim it is holding.

    ⚠ **``source`` used to be ``scheme``**, selecting a tag glued onto the
    digest. Both rungs now hash under ``HASH_SCHEME`` — the KEY GENERATION,
    not a provenance marker — and provenance is a member that survives every
    mode; see ``hash_principal_id``.

    **All three members or nothing**, and structurally so: there is exactly
    ONE ``PrincipalWire(...)`` in this function, at the bottom, and every
    branch that cannot produce a value returns ``None`` before reaching it.
    SPEC §11.4 makes a partial object malformed, so "we know who but not how"
    is not a state this may emit — and that is the same standard the paragraph
    above holds the chokepoint to, rather than two construction sites agreeing.
    """
    if principal is None:
        return None

    # A mode this function does not recognise cannot be given a truthful
    # ``form``, and guessing one is the failure the member exists to prevent —
    # ``form`` is what a consumer classifies on.
    form = _FORM_BY_MODE.get(mode)
    if form is None:
        # Once per install, not once per call, for the same reason the
        # missing-key branch below is throttled — and more so here: `mode` is
        # fixed for the life of the process, so this cannot stop repeating
        # once it starts. (The hashing-failure branch is deliberately NOT
        # throttled; that one can vary per call.)
        if "unknown_mode" not in warned:
            warned.add("unknown_mode")
            logger.warning(
                "baton: unknown principal_id_mode %r — dropping the principal "
                "(events still emit). Expected one of %s.",
                mode,
                sorted(PRINCIPAL_ID_MODES),
            )
        return None

    if mode == PRINCIPAL_ID_MODE_RAW:
        # Verbatim, and deliberately NOT through the vendor's scrubber. A
        # scrubber that redacts emails maps every distinct user onto one
        # redaction constant, which merges all of them into a single actor —
        # the correlation bug this field exists to fix, delivered silently by
        # the privacy feature. Raw mode is an explicit opt-in to carrying PII;
        # scrubbing it would not make it private, only wrong.
        #
        # No canonicalization either. NFC+strip+lower exists so every modality
        # HASHES an identical value; applied to a displayed one it corrupts
        # case-sensitive subjects. And no issuer is folded in: this value is
        # meant to be read by a human, and concatenating an issuer URL onto it
        # defeats the only reason to choose this mode. The cost is the exact
        # collision the issuer was folded in to prevent — two identity
        # providers, one `sub` — which raw mode accepts by construction.
        #
        # ⚠ What raw mode NO LONGER forfeits is the provenance: `source` is a
        # member now and rides every mode, so a consumer here is told a real
        # identity AND which mechanism named it.
        value = principal.principal_id[:RAW_PRINCIPAL_ID_MAX_LEN]
    elif hmac_key is None:
        if "no_hmac_key" not in warned:
            warned.add("no_hmac_key")
            # Never log the principal itself. This line exists because identity
            # was configured and silently produced nothing; printing the value
            # to explain that would put the raw identity in the vendor's log
            # files, which is the residency leak one layer sideways.
            logger.warning(
                "baton: identity resolved but no principal HMAC key is set — "
                "dropping the principal (events still emit). Set "
                "BATON_PRINCIPAL_ID_HMAC_KEY, or pass "
                "VendorConfig(principal_id_hmac_key=...), to attach it."
            )
        return None

    else:
        try:
            value = hash_principal_id(
                principal.principal_id,
                tenant_id=tenant_id,
                key=hmac_key,
                issuer=principal.issuer,
            )
        except Exception:
            # The docstring above says nothing here may raise; this is the
            # call that could. ``hmac.new`` rejects a non-bytes key, and while
            # the public path now coerces and validates at install, this
            # function is reachable by constructing an adapter directly. A
            # guard costs nothing and makes the contract literally true rather
            # than nearly true.
            logger.warning("baton: principal hashing failed; dropping the principal", exc_info=True)
            return None

    # The ONE construction site. Every branch above either set ``value`` or
    # returned ``None``, so "all three members or nothing" is a property of
    # the control flow rather than of two sites agreeing to pass the same
    # three arguments.
    return PrincipalWire(id=value, source=source, form=form)


async def resolve_call_principal(
    token: Any,
    *,
    hook: ResolvePrincipalHook | None,
    hook_context: SessionResolutionContext | None,
    mode: str,
    tenant_id: str,
    hmac_key: bytes | None,
    logger: logging.Logger,
    warned: set[str],
) -> PrincipalWire | None:
    """The envelope's ``principal`` for one call — both provenances, in order.

    **Rung 0: the vendor's ``resolve_principal`` hook.** ``source: "asserted"``.
    **Rung 1: the verified access token.** ``source: "attested"``.
    ``None`` when neither resolves, which is the common case and never an
    error. SPEC §11.4 carries the same ladder and the consumer-side rules.

    **The hook sits ABOVE the token, and that is a decision rather than an
    implementation detail.** Attested normally beats asserted — but a gateway's
    token names whatever principal the gateway authenticated, which is
    frequently a service account rather than the person, while a hook exists
    only where a vendor deliberately wrote one for this purpose. The more
    specific claim wins over the better-verified one, and ``source`` is what
    keeps that honest downstream: a consumer is never told an assertion was
    verified, it is told which it got and decides for itself.

    ⚠ **The two rungs are BYTE-IDENTICAL for one ``(tenant_id, principal,
    issuer)``**, so ``source`` is the only thing separating them — a consumer
    still reading the prefix sees one actor where there are two claims of
    different weight. Why that is so: ``hash_principal_id``.

    ⚠ **The hook is not consulted when it is not configured, and that path must
    stay free.** ``hook_context`` is built by the caller only when ``hook`` is
    not ``None`` — header extraction is not free on every tool call of every
    server that will never set this field. A configured hook with a ``None``
    context is treated as no hook rather than as an error, so a caller that
    forgets the context degrades to today's behaviour instead of losing
    identity outright.

    Fail-open throughout, like everything on this path: a hook that raises,
    returns the wrong type, or returns ``None`` falls through to the token
    exactly as if it had not been configured.

    ⚠ **One case deliberately does NOT fall through: a hook that returned a
    usable principal the SDK then could not hash** (a ``principal_id`` carrying an
    unpaired surrogate is the reachable shape; a non-string ``issuer`` is
    coerced away before it gets here). That emits NO ``principal`` rather than
    the token's. The difference from the cases above is what the hook said: a
    hook returning ``None`` has no opinion about this request, so the token is
    the best available answer — but a hook that named a person and failed to
    render them has told us the token names somebody ELSE, which is the whole
    reason it sits above the token. Substituting the gateway's service account
    there would file the call under a plausible, wrong, and heavily-merged
    actor. Losing the join beats inventing one (CHARTER, the D2 join rule),
    and an absent ``principal`` is already the common, well-handled case.
    """
    if hook is not None and hook_context is not None:
        principal = await resolve_principal_via_hook(hook, hook_context, logger=logger)
        if principal is not None:
            return _finish_principal(
                principal,
                mode=mode,
                tenant_id=tenant_id,
                hmac_key=hmac_key,
                source=PRINCIPAL_SOURCE_ASSERTED,
                logger=logger,
                warned=warned,
            )
    return resolve_attested_principal(
        token,
        mode=mode,
        tenant_id=tenant_id,
        hmac_key=hmac_key,
        logger=logger,
        warned=warned,
    )
