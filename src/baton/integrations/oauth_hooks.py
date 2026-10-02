"""Ready-made ``resolve_principal`` hooks for a vendor whose server runs OAuth.

Pass one as ``VendorConfig(resolve_principal=...)``. Each reads a claim from
``SessionResolutionContext.claims`` — the verified access token's claims — and
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

⚠ **Nothing on ``mcp < 1.27``**, whose ``AccessToken`` has no ``claims``:
``context.claims`` is ``None`` there and both hooks return ``None``. See
``identity_adapter.token_claims`` for the vendor-subclass escape hatch.

Both run inline rather than on a worker thread (``_hooks.runs_inline``): they
are a dict lookup, and cannot block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton.identity import Principal
from baton.integrations._hooks import runs_inline

if TYPE_CHECKING:
    from baton.integrations._config import SessionResolutionContext


@runs_inline
def principal_from_oauth_sub(context: SessionResolutionContext) -> Principal | None:
    """The token's ``sub``, keyed with its ``iss``.

    ``sub`` is unique only per issuer, which is why ``iss`` rides along: two
    identity providers can hand two different people the same subject.
    """
    claims = context.claims
    if claims is None:
        return None
    sub = claims.get("sub")
    # ``.strip()``, not truthiness: the hash canonicalizes NFC → strip → lower,
    # so a whitespace-only subject would be one phantom actor every such
    # caller merges into.
    if not isinstance(sub, str) or not sub.strip():
        return None
    # Coerced HERE as well as in the runner: these are public exports, and a
    # vendor wrapping one reads ``.issuer`` before the runner ever sees it.
    issuer = claims.get("iss")
    return Principal(
        principal_id=sub, issuer=issuer if isinstance(issuer, str) and issuer else None
    )


@runs_inline
def principal_from_oauth_email(context: SessionResolutionContext) -> Principal | None:
    """The token's ``email`` claim, as the WHOLE address and with no issuer.

    The local part alone is not an id — ``alice@acme.com`` and
    ``alice@contoso.com`` are two people. It is returned as ``user_name``,
    which is what the hook HANDS BACK and nothing more: nothing in the SDK
    reads it and it is sent nowhere.

    **No issuer**, unlike the ``sub`` hook. A subject is unique only per
    issuer; an address is unique on its own. Folding ``iss`` in would give one
    person a new pseudonym the day their identity provider changes its issuer
    URL (a v1 → v2 endpoint, a custom domain) or they sign in through a second
    one — the actor split keying on email exists to avoid.

    ⚠ **``email`` is not a standard ACCESS-token claim.** OIDC puts it in the ID
    token; it is in ``claims`` only if the vendor's identity provider adds it
    and their ``TokenVerifier`` keeps it. Absent, this returns ``None``.

    ``email_verified`` is not consulted. Whether an unverified address is good
    enough to group a person by is the vendor's call, and a vendor who says no
    writes a three-line hook that checks it.
    """
    claims = context.claims
    if claims is None:
        return None
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    local, at, _domain = email.rpartition("@")
    return Principal(principal_id=email, user_name=local if at and local else None)
