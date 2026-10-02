"""End-user identity off a vendor's hook — resolve the principal, hash it at
the edge.

Shared by BOTH adapters, beside ``runtime_adapter.py`` and for the same reason:
the last capture signal that lived under one adapter's package was the one the
other adapter never called, and it shipped ``unknown`` on every event for two
releases before anybody noticed.

**Identity comes from the vendor's ``resolve_principal`` hook and from nowhere
else.** Until this release the SDK also read ``claims["sub"]`` off the verified
OAuth access token on its own whenever no hook answered, and stamped it
``principal.source == "attested"``. That rung is GONE (SPEC §11.4, §13): who
the person behind a call is, and which claim names them, is the vendor's
decision, and a default the vendor never chose was making it for them — a
gateway's token frequently names a service account, and the SDK cannot tell.
A vendor who wants the token's subject or email passes one of the ready-made
hooks in ``oauth_hooks.py``; one who wants anything else writes their own.
Every principal this module emits is therefore ``source: "asserted"``.

Keep all of it apart from ``agent_runtime``: that is what a client says it is,
and this is who the vendor says the person is. They answer different questions.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from types import MappingProxyType
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

#: Emit the HMAC of the principal. The console sees the bare ``<hex>`` (``h1:``
#: -tagged before 0.8.11) and never the
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
#: is the provenance, fixed at ``"asserted"`` for this producer.
_FORM_BY_MODE = {
    PRINCIPAL_ID_MODE_HASHED: "hashed",
    PRINCIPAL_ID_MODE_RAW: "raw",
}

PRINCIPAL_ID_MODES = frozenset(_FORM_BY_MODE)

#: ``principal.source`` — a vendor's own per-request resolver. The vendor
#: states who this is and nothing in the protocol checked the claim, including
#: when the hook read it off a verified token: the SDK cannot see what the hook
#: did. The only value this SDK emits (SPEC §11.4); ``"attested"`` stays
#: registered for stored events and is produced by nobody.
PRINCIPAL_SOURCE_ASSERTED = "asserted"

#: Cap on a RAW principal. Same posture and same number as the declared
#: ``agent_runtime`` tiers: it is external text copied onto every event of the
#: call, so it gets a bound. Hashed values are fixed-width by construction and
#: are not capped — truncating a digest would destroy the join.
RAW_PRINCIPAL_ID_MAX_LEN = 128


#: A vendor's per-request identity resolver. Takes the adapter-neutral
#: ``SessionResolutionContext`` — headers, meta, tool name, arguments and the
#: verified token's claims — so
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

    Never raises. An exception is logged and treated as a miss, so the event
    ships without a ``principal`` exactly as if no hook were configured — ``principal`` is additive analytics and a vendor's own bug in
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
        logger.warning(
            "baton: resolve_principal hook raised; the event ships without a principal",
            exc_info=True,
        )
        return None
    if result is None:
        return None
    if not isinstance(result, Principal):
        logger.warning(
            "baton: resolve_principal hook returned %s, not a baton.Principal — "
            "ignoring it; the event ships without a principal. Return "
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
            "non-string principal_id — ignoring it."
        )
        return None
    # ``issuer`` is coerced HERE, for every hook including the shipped OAuth
    # ones (which pass ``claims["iss"]`` through raw), for two reasons that are
    # both bugs without it.
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
    #     downstream, but the cost is the whole event's ``principal``.
    #     Coercing here means a junk issuer costs the issuer, not the identity.
    if not isinstance(result.issuer, str) or not result.issuer:
        result = replace(result, issuer=None)
    return result


def token_claims(token: Any) -> Mapping[str, Any] | None:
    """The verified token's claims, or ``None``. Never raises.

    ``token`` is whatever the adapter's ``get_access_token()`` returned —
    ``None`` on most requests and every stdio one. Called by all four context
    construction sites, so both adapters hand a hook the same dict.

    ⚠ **``getattr``, because ``claims`` does not exist on ``mcp < 1.27``.** On
    1.20 and 1.25 the model carries only ``token``/``client_id``/``scopes``/
    ``expires_at``/``resource``, and Pydantic's default ``extra="ignore"``
    SILENTLY DROPS a ``claims=…`` a verifier passes (measured, not read). A
    vendor whose verifier returns an ``AccessToken`` SUBCLASS declaring
    ``claims`` is read correctly on every version.

    Broad ``except``: this reads attributes off an object a VENDOR's verifier
    built, outside the hook runner's boundary, and an identity read may not
    fail a tool call.

    **A read-only VIEW**, not the token's own dict: shipped hooks run inline
    on the request, and a hook normalizing in place would otherwise rewrite
    the claims the vendor's tool handler reads next. A proxy, not a copy —
    it blocks the write without allocating per call.
    """
    if token is None:
        return None
    try:
        claims = getattr(token, "claims", None)
        if not isinstance(claims, Mapping):
            return None
        return MappingProxyType(claims)
    except Exception:
        return None


def _finish_principal(
    principal: Principal | None,
    *,
    mode: str,
    tenant_id: str,
    hmac_key: bytes | None,
    logger: logging.Logger,
    warned: set[str],
) -> PrincipalWire | None:
    """Turn a resolved principal into the finished wire object, or ``None``.

    **The edge-hash chokepoint.** The raw value becomes its final wire form
    here and no caller downstream ever holds it, mirroring baton-proxy's
    ``Emitter._enqueue`` discipline — the rule that keeps raw identity from
    reaching a console-bound sink by some path nobody audited.

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
    return PrincipalWire(id=value, source=PRINCIPAL_SOURCE_ASSERTED, form=form)


async def resolve_call_principal(
    *,
    hook: ResolvePrincipalHook | None,
    hook_context: SessionResolutionContext | None,
    mode: str,
    tenant_id: str,
    hmac_key: bytes | None,
    logger: logging.Logger,
    warned: set[str],
) -> PrincipalWire | None:
    """The envelope's ``principal`` for one call: the vendor's hook, or nothing.

    ``None`` when no hook is configured or it has no answer, which is the
    common case and never an error. SPEC §11.4 carries the consumer-side rules.

    ⚠ **There is no fallback, and that is the decision rather than a gap.**
    This used to fall through to the verified token's ``sub``. A vendor who
    wants that passes ``baton.principal_from_oauth_sub`` as the hook, which reads
    the same claim off ``context.claims`` — so the only thing that
    changed is who chose it.

    ⚠ **The hook is not consulted when it is not configured, and that path must
    stay free.** ``hook_context`` is built by the caller only when ``hook`` is
    not ``None`` — header extraction and the token read are not free on every
    tool call of every server that will never set this field. A configured
    hook with a ``None`` context is treated as no hook rather than as an error.

    Fail-open throughout, like everything on this path: a hook that raises,
    returns the wrong type, or returns ``None`` costs the event its
    ``principal`` and nothing else.
    """
    if hook is None or hook_context is None:
        return None
    principal = await resolve_principal_via_hook(hook, hook_context, logger=logger)
    return _finish_principal(
        principal,
        mode=mode,
        tenant_id=tenant_id,
        hmac_key=hmac_key,
        logger=logger,
        warned=warned,
    )
