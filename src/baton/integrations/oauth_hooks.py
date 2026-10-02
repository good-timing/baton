"""Ready-made ``resolve_principal`` hooks for a vendor whose server runs OAuth.

Pass one as ``VendorConfig(resolve_principal=...)``. Each reads a claim off the
verified access token the adapter put on ``SessionResolutionContext`` and
returns a ``Principal``, or ``None`` when the claim is not there — so a vendor
can also call them from a hook of their own and fall back to something else::

    def resolve(ctx):
        return principal_from_oauth_email(ctx) or principal_from_oauth_sub(ctx)

They are ordinary hooks and carry no standing an own hook lacks: what they
return is ``principal.source == "asserted"`` like any other (SPEC §11.4).

**HTTP only.** MCP auth is ASGI middleware, so a stdio request has no token and
both hooks return ``None`` there. A stdio vendor who knows the user out of band
writes their own hook.

⚠ **Never ``client_id``.** It names the OAuth APPLICATION, not the person —
measured identical (``acme-desktop-app``) for two different users across every
mcp version tested. Keying on it merges every user of one app into one actor.
It looks user-shaped in a naive test only because ``JWTVerifier`` falls back
``client_id ?? azp ?? sub``.

⚠ **``claims``, not ``subject``.** ``claims["sub"]`` was populated on every
combination measured (2026-09-07), while ``subject`` is ``None`` for every user
across the whole fastmcp 2.x/3.x band and is DROPPED by fastmcp 3.4.2's own
``AccessToken`` rebuild.

⚠ **``claims`` does not exist on ``mcp < 1.27``.** On 1.20 and 1.25 the model
carries only ``token``/``client_id``/``scopes``/``expires_at``/``resource``, and
Pydantic's default ``extra="ignore"`` SILENTLY DROPS a ``claims=…`` a verifier
passes (measured, not read). Hence ``getattr``: on that band these hooks return
``None``, while a vendor who declares ``claims`` on an ``AccessToken`` SUBCLASS
is read correctly on every version.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baton.identity import Principal

if TYPE_CHECKING:
    from baton.integrations._config import SessionResolutionContext


def _claims(context: SessionResolutionContext) -> dict[str, Any] | None:
    """The verified token's claims, or ``None``. Never raises.

    Broad on purpose: this reads attributes off an object a VENDOR's verifier
    constructed, and a hook that raises costs the event its principal. The
    hook runner would catch it, but logging a stack trace on every call of a
    server whose verifier builds a slightly odd token is noise, not a signal.
    """
    try:
        claims = getattr(context.access_token, "claims", None)
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def _issuer(claims: dict[str, Any]) -> str | None:
    """``iss`` when it is a non-empty string, else ``None``.

    ``""`` and ``None`` hash DIFFERENTLY (the issuer is folded into the HMAC
    only when it is not ``None``), so passing an empty one through would give
    the same person a second pseudonym the day the verifier starts omitting it.
    """
    issuer = claims.get("iss")
    return issuer if isinstance(issuer, str) and issuer else None


def principal_from_oauth_sub(context: SessionResolutionContext) -> Principal | None:
    """The token's ``sub``, keyed with its ``iss``.

    ``sub`` is unique only per issuer, which is why ``iss`` rides along: two
    identity providers can hand two different people the same subject.
    """
    claims = _claims(context)
    if claims is None:
        return None
    sub = claims.get("sub")
    # ``.strip()``, not truthiness: the hash canonicalizes NFC → strip → lower,
    # so a whitespace-only subject would be one phantom actor every such
    # caller merges into.
    if not isinstance(sub, str) or not sub.strip():
        return None
    return Principal(principal_id=sub, issuer=_issuer(claims))


def principal_from_oauth_email(context: SessionResolutionContext) -> Principal | None:
    """The token's ``email`` claim, keyed with its ``iss``.

    ``principal_id`` is the WHOLE address and ``user_name`` is the part before
    the last ``@``. The local part alone is not an id — ``alice@acme.com`` and
    ``alice@contoso.com`` are two people — so it rides as the name, which is
    PII confined to the payload tier and never reaches the console.

    ⚠ **``email`` is not a standard ACCESS-token claim.** OIDC puts it in the ID
    token; it is in ``claims`` only if the vendor's identity provider adds it
    and their ``TokenVerifier`` keeps it. Absent, this returns ``None``.

    ``email_verified`` is not consulted. Whether an unverified address is good
    enough to group a person by is the vendor's call, and a vendor who says no
    writes a three-line hook that checks it.
    """
    claims = _claims(context)
    if claims is None:
        return None
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    local, at, _domain = email.rpartition("@")
    return Principal(
        principal_id=email,
        user_name=local if at and local else None,
        issuer=_issuer(claims),
    )
