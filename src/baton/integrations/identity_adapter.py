"""End-user identity off the MCP auth seam — resolve the principal, hash it
at the edge.

Shared by BOTH adapters, beside ``runtime_adapter.py`` and for the same reason:
the last capture signal that lived under one adapter's package was the one the
other adapter never called, and it shipped ``unknown`` on every event for two
releases before anybody noticed.

**This is the attested half of identity.** ``agent_runtime`` is what a client
says it is — self-reported, never verified, and a client may call itself
anything. ``user_id`` is derived from a bearer token the VENDOR's own verifier
already validated, so it is the one identity claim on the envelope that
something checked. Keep the two apart; they answer different questions and they
are trustworthy to different degrees.

**Transport reality: this is HTTP-only, on every supported version.** MCP auth
is ASGI middleware (``mcp.server.auth.middleware.bearer_auth`` operates on a
Starlette ``Scope``), and ``get_access_token()`` reads a contextvar that
middleware sets. On stdio nothing sets it, so there is no token, no principal
and no ``user_id`` — not a failure, just the shape of the transport. A vendor
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
from typing import Any

from baton.identity import Principal, hash_user_id

#: Emit the HMAC of the principal. The console sees ``h1:<hex>`` and never the
#: raw identity — the residency contract (the console DB is metadata-only) and
#: the default.
USER_ID_MODE_HASHED = "hashed"

#: Emit the principal VERBATIM. A deliberate opt-in that puts real end-user
#: identity on the wire and into the console database. Correct for a vendor
#: dogfooding their own server, or one with no residency obligation who wants
#: to read a name instead of a hash; wrong by default, which is why it is not
#: the default.
USER_ID_MODE_RAW = "raw"

USER_ID_MODES = frozenset({USER_ID_MODE_HASHED, USER_ID_MODE_RAW})

#: Cap on a RAW principal. Same posture and same number as the declared
#: ``agent_runtime`` tiers: it is external text copied onto every event of the
#: call, so it gets a bound. Hashed values are fixed-width by construction and
#: are not capped — truncating a digest would destroy the join.
RAW_USER_ID_MAX_LEN = 128


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
        if not isinstance(sub, str) or not sub:
            return None
        issuer = claims.get("iss")
        if not isinstance(issuer, str) or not issuer:
            issuer = None
        return Principal(user_id=sub, issuer=issuer)
    except (AttributeError, TypeError, ValueError):
        return None


def resolve_user_id(
    token: Any,
    *,
    mode: str,
    tenant_id: str,
    hmac_key: bytes | None,
    logger: logging.Logger,
    warned: set[str],
) -> str | None:
    """Resolve a verified token into the envelope's ``user_id``, or ``None``.

    **This is the edge-hash chokepoint.** The raw principal is turned into its
    final wire form HERE and the finished string is what travels onward — no
    caller downstream of this function ever holds the raw value. That mirrors
    baton-proxy's ``Emitter._enqueue`` discipline, where the same rule is what
    keeps raw identity from reaching a console-bound sink by some path nobody
    audited.

    ``warned`` is a caller-owned set used to log the missing-key case exactly
    once per install rather than once per tool call.

    Fail-open throughout — every branch that cannot produce a value returns
    ``None`` and the event ships without the field:

    - no auth on the request (or stdio, where there is none) → ``None``
    - ``mcp < 1.27`` with no ``claims`` on the token → ``None``
    - ``hashed`` with no HMAC key configured → ``None``, warned once
    - ``raw`` → the subject verbatim, no key needed

    ``user_id`` is additive analytics. It is never a consent or authorization
    gate, so nothing here may raise, and nothing here may stop an event.
    """
    principal = principal_from_access_token(token)
    if principal is None:
        return None

    if mode == USER_ID_MODE_RAW:
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
        return principal.user_id[:RAW_USER_ID_MAX_LEN]

    if hmac_key is None:
        if "no_hmac_key" not in warned:
            warned.add("no_hmac_key")
            # Never log the principal itself. This line exists because identity
            # was configured and silently produced nothing; printing the value
            # to explain that would put the raw identity in the vendor's log
            # files, which is the residency leak one layer sideways.
            logger.warning(
                "baton: identity resolved but no user_id HMAC key is set — "
                "dropping user_id (events still emit). Set "
                "BATON_USER_ID_HMAC_KEY, or pass "
                "VendorConfig(user_id_hmac_key=...), to attach it."
            )
        return None

    return hash_user_id(
        principal.user_id,
        tenant_id=tenant_id,
        key=hmac_key,
        issuer=principal.issuer,
    )
