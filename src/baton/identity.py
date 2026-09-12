"""End-user identity — resolve a raw principal, hash it at the edge.

Baton attaches an end-user actor (``user_id``) to every event so the Console
can answer "which *customer* hit this" and group by
``(tenant_id, vendor_id, user_id)``.

Residency contract: the Console DB is metadata-only and may only ever see the
HASH — raw identity must never leave the capture edge. So hashing happens HERE,
before an event reaches any console-bound sink.

Two pieces:

- ``hash_user_id`` — the per-tenant HMAC. Zero new deps (stdlib
  ``hmac``/``hashlib``/``unicodedata``).
- ``IdentityResolver`` / ``Principal`` — the per-modality seam. Each capture
  modality ships a resolver that turns its native carrier into a ``Principal``;
  the core only ever receives the raw principal and hashes it.

Ported from ``baton_proxy.identity`` — keep the two copies in lockstep until
the shared package lands (same discipline as ``scrub.py``).

⚠ **The copies are OUT of lockstep as of 2026-09-09, deliberately and in one
direction.** ``hash_user_id`` here takes an optional ``issuer``; the proxy copy
does not yet. The default is what keeps that safe: ``issuer=None`` produces the
identical message the proxy produces, so the two agree on every hash either has
ever emitted, and only issuer-bearing hashes — values that did not exist before
today — are unique to this copy. Adopting the same signature in
``baton_proxy.identity`` is a tracked follow-up; until it lands, do not change
the message layout again, because a second divergence would not have a
compatible default to hide behind.

⚠ **A SECOND signature divergence landed 2026-09-11: ``scheme``.** Same
discipline and the same reason it is safe — ``scheme`` defaults to
``HASH_SCHEME``, so the proxy's issuer-less, scheme-less calls and this
copy's produce the identical string. It does NOT touch the HMAC message, so
it cannot move a digest the way a layout change would; only the tag in front
of it differs. Both divergences are additive keyword-only parameters with
pre-existing behaviour as their default, and that is the only form a
divergence here may take.
"""

from __future__ import annotations

import hmac
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

# Scheme tag prefixed onto every hash. This is the ROTATION seam: rotate the
# HMAC key by cutting new hashes to ``h2:`` while historical events stay under
# ``h1:``. A single principal produces different hashes across the rotation
# boundary — an accepted, documented discontinuity (the raw value was never
# stored, so it can't be re-hashed).
HASH_SCHEME = "h1"

#: Scheme tag for a principal a VENDOR ASSERTED rather than one an identity
#: provider attested — the ``VendorConfig.resolve_user`` hook's output. It sits
#: OUTSIDE the ``h*`` family on purpose: ``h2:`` is spoken for by the rotation
#: seam above, and rotation must keep every letter it may later want. Sharing
#: ``h1:`` was the alternative and it is the one thing this tag exists to
#: prevent — an assertion and an attestation rendering identically downstream,
#: so a console cannot say which one it is showing (SPEC §11.4).
#:
#: ⚠ Both tags are in SPEC §11.4's REGISTERED SET, and that set — not the mere
#: presence of a prefix — is what tells a consumer a value is a pseudonym. A
#: raw principal can legitimately look tagged (``mailto:``, ``acct:``, ``urn:``,
#: ``https:`` are all real OIDC subject forms), so a structural test reads a
#: live email address as safe. Adding a scheme here is a SPEC change.
VENDOR_HASH_SCHEME = "v1"


@dataclass(frozen=True)
class Principal:
    """A resolved end-user identity, RAW (pre-hash).

    Only ``user_id`` is hashed onto the wire today. ``user_name`` / ``user_data``
    are PII confined to the customer-owned payload tier — they are NOT emitted
    to the console path today and are force-scrubbed out of payloads (see
    scrub ``REDACT_FIELD_NAMES``).
    """

    user_id: str
    user_name: str | None = None
    user_data: dict[str, Any] | None = None
    issuer: str | None = None
    """The identity provider that minted the principal (the OIDC ``iss``
    claim), when one is known.

    Carried because ``user_id`` alone is **unique only per issuer** — the
    ``mcp`` SDK says so in its own ``AccessToken.subject`` comment. A vendor
    running two identity providers can legitimately have two different people
    arrive under the same ``sub``, and hashing ``sub`` alone would collapse
    them into one actor. Folding the issuer in is what makes the pair globally
    unique.

    Optional, and ``None`` for every modality that has no notion of an issuer
    (a gateway header, a static env principal). ``None`` hashes exactly as
    this function always has — see ``hash_user_id``."""


class IdentityResolver(Protocol):
    """Turns a modality-native carrier into a ``Principal``. Returns ``None``
    when no identity is available — the core then skips ``user_id``
    (fail-open).

    ⚠ **This is NOT the shape a vendor implements.** The vendor-facing seam is
    ``VendorConfig.resolve_user`` — a plain callable taking the adapter-neutral
    ``SessionResolutionContext``, which is the hook vendors write. A
    method-on-an-object Protocol taking an
    untyped ``carrier`` would be a second convention for the same job, and the
    ``carrier`` would have to be one of the two libraries' incompatible
    ``Context`` types — the precise coupling ``SessionResolutionContext`` was
    introduced to avoid.

    It stays because ``baton_proxy.identity`` carries this Protocol verbatim
    and the two copies are kept in lockstep; deleting it here diverges them for
    no gain. The return contract — ``Principal | None``, never raising — is
    what ``resolve_user`` honours."""

    def resolve(self, carrier: Any) -> Principal | None: ...


def _canonicalize(raw_principal: str) -> str:
    """Pin the principal string once, centrally, so every modality hashes an
    identical value. NFC-normalize, strip, lowercase."""
    return unicodedata.normalize("NFC", raw_principal).strip().lower()


def hash_user_id(
    raw_principal: str,
    *,
    tenant_id: str,
    key: bytes,
    issuer: str | None = None,
    scheme: str = HASH_SCHEME,
) -> str:
    """HMAC-SHA256 a raw principal into a console-safe, per-tenant ``user_id``.

    ``tenant_id`` is folded into the HMAC MESSAGE (not just the key) so the same
    principal under two tenants can never collide or be cross-tenant-correlated.
    Returns ``"<scheme>:<hex>"`` (e.g. ``"h1:9f2c…"``).

    ``issuer`` — the OIDC ``iss`` claim — is folded in the same way when
    supplied, because a ``sub`` is unique only within the provider that minted
    it (RFC 7519 §4.1.2; the ``mcp`` SDK repeats the caveat on
    ``AccessToken.subject``). Two identity providers behind one vendor can hand
    out the same ``sub`` to different people, and without the issuer those two
    people hash to one ``user_id`` — a silent merge of exactly the kind this
    project keeps finding.

    ``scheme`` tags the DERIVATION, and only the tag changes — the digest for a
    given ``(tenant_id, principal, issuer)`` is identical under every scheme,
    because the tag is not part of the HMAC message. That is deliberate: the
    same person reached by two provenances is meant to be recognisably the same
    hex under two tags, not two unrelated values, so a consumer that decides to
    unify them downstream can, and one that must keep them apart still can.
    ``VENDOR_HASH_SCHEME`` is the asserted-principal tag; the default is the
    attested one and is what every pre-existing caller gets.

    ⚠ **``issuer=None`` MUST hash byte-identically to the pre-issuer form**, and
    the append-only message layout below is what guarantees it. Every hash
    baton-proxy and baton-extmcp have produced since 0.5.0 was issuer-less, and
    they share this function's contract as parity mirrors — so a format change
    under the same ``h1:`` tag would leave one derivation tag naming two
    different derivations across the family, which is precisely what the scheme
    prefix exists to prevent. Issuer-bearing hashes are new values that never
    existed before; nothing needs migrating.
    """
    message = f"{tenant_id}\x00{_canonicalize(raw_principal)}"
    if issuer is not None:
        message += f"\x00{_canonicalize(issuer)}"
    digest = hmac.new(key, message.encode(), sha256).hexdigest()
    return f"{scheme}:{digest}"
