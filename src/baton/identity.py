"""Principal identity — resolve a raw principal, hash it at the edge.

Baton attaches the resolved principal (``principal``) to every event so the
Console can group by ``(tenant_id, vendor_id, principal.id)``, at whatever grain
the vendor resolved (SPEC §11.4).

Residency contract: the Console DB is metadata-only and may only ever see the
HASH — raw identity must never leave the capture edge. So hashing happens HERE,
before an event reaches any console-bound sink.

Two pieces:

- ``hash_principal_id`` — the per-tenant HMAC. Zero new deps (stdlib
  ``hmac``/``hashlib``/``unicodedata``).
- ``IdentityResolver`` / ``Principal`` — the per-modality seam. Each capture
  modality ships a resolver that turns its native carrier into a ``Principal``;
  the core only ever receives the raw principal and hashes it.

Ported from ``baton_proxy.identity`` — keep the two copies in lockstep until
the shared package lands (same discipline as ``scrub.py``).

⚠ **The copies are OUT of lockstep as of 2026-09-09, deliberately and in one
direction.** ``hash_principal_id`` here takes an optional ``issuer``; the proxy copy
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

# ⚠ ``VENDOR_HASH_SCHEME = "v1"`` lived here and is RETIRED (SPEC §13).
#
# It tagged a principal a VENDOR ASSERTED rather than one an identity provider
# attested, so that "a console cannot say which one it is showing" could not
# happen. The job was real; the carrier was wrong. A tag can only say it in
# ``"hashed"`` mode — ``"raw"`` emits no tag at all — so the one mode that puts
# a REAL identity on the wire was the one that dropped its provenance, and no
# consumer could recover it. Provenance is now ``principal.source``, a member
# that rides every mode.
#
# Retiring it was a RELABEL, not a recomputation: the tag was never part of the
# HMAC message (see ``hash_principal_id``), so an asserted principal's digest
# was always byte-identical to an attested one's for the same inputs. What
# changes on the wire is the three characters in front of the hex — which SPEC
# §13 records as a value change, because a consumer comparing whole id strings
# across the upgrade sees one actor become two for every vendor that had
# configured ``resolve_principal``.
#
# ⚠ **Do not reintroduce a provenance tag here.** The remaining ``h<n>:`` says
# which HMAC KEY GENERATION produced the digest and nothing else. A second
# meaning on that prefix is the exact joining this change undid.


@dataclass(frozen=True)
class Principal:
    """A resolved principal, RAW (pre-hash) — a person, a service account or an
    organisation, whichever the resolver can honestly name.

    Only ``principal_id`` is hashed onto the wire today. ``user_name`` / ``user_data``
    are PII confined to the customer-owned payload tier — they are NOT emitted
    to the console path today and are force-scrubbed out of payloads (see
    scrub ``REDACT_FIELD_NAMES``).
    """

    principal_id: str
    user_name: str | None = None
    user_data: dict[str, Any] | None = None
    issuer: str | None = None
    """The identity provider that minted the principal (the OIDC ``iss``
    claim), when one is known.

    Carried because ``principal_id`` alone is **unique only per issuer** — the
    ``mcp`` SDK says so in its own ``AccessToken.subject`` comment. A vendor
    running two identity providers can legitimately have two different people
    arrive under the same ``sub``, and hashing ``sub`` alone would collapse
    them into one actor. Folding the issuer in is what makes the pair globally
    unique.

    Optional, and ``None`` for every modality that has no notion of an issuer
    (a gateway header, a static env principal). ``None`` hashes exactly as
    this function always has — see ``hash_principal_id``."""


class IdentityResolver(Protocol):
    """Turns a modality-native carrier into a ``Principal``. Returns ``None``
    when no identity is available — the core then omits ``principal``
    (fail-open).

    ⚠ **This is NOT the shape a vendor implements.** The vendor-facing seam is
    ``VendorConfig.resolve_principal`` — a plain callable taking the adapter-neutral
    ``SessionResolutionContext``, which is the hook vendors write. A
    method-on-an-object Protocol taking an
    untyped ``carrier`` would be a second convention for the same job, and the
    ``carrier`` would have to be one of the two libraries' incompatible
    ``Context`` types — the precise coupling ``SessionResolutionContext`` was
    introduced to avoid.

    It stays because ``baton_proxy.identity`` carries this Protocol verbatim
    and the two copies are kept in lockstep; deleting it here diverges them for
    no gain. The return contract — ``Principal | None``, never raising — is
    what ``resolve_principal`` honours."""

    def resolve(self, carrier: Any) -> Principal | None: ...


def _canonicalize(raw_principal: str) -> str:
    """Pin the principal string once, centrally, so every modality hashes an
    identical value. NFC-normalize, strip, lowercase."""
    return unicodedata.normalize("NFC", raw_principal).strip().lower()


def hash_principal_id(
    raw_principal: str,
    *,
    tenant_id: str,
    key: bytes,
    issuer: str | None = None,
    scheme: str = HASH_SCHEME,
) -> str:
    """HMAC-SHA256 a raw principal into a console-safe, per-tenant ``principal.id``.

    ``tenant_id`` is folded into the HMAC MESSAGE (not just the key) so the same
    principal under two tenants can never collide or be cross-tenant-correlated.
    Returns ``"<scheme>:<hex>"`` (e.g. ``"h1:9f2c…"``).

    ``issuer`` — the OIDC ``iss`` claim — is folded in the same way when
    supplied, because a ``sub`` is unique only within the provider that minted
    it (RFC 7519 §4.1.2; the ``mcp`` SDK repeats the caveat on
    ``AccessToken.subject``). Two identity providers behind one vendor can hand
    out the same ``sub`` to different people, and without the issuer those two
    people hash to one ``principal.id`` — a silent merge of exactly the kind this
    project keeps finding.

    ``scheme`` names the HMAC KEY GENERATION, and only the tag changes — the
    digest for a given ``(tenant_id, principal, issuer)`` is identical under
    every scheme, because the tag is not part of the HMAC message. It exists
    for ROTATION: cutting the secret moves new hashes to ``h2:`` while
    historical ones keep ``h1:``, so a consumer comparing two values knows they
    are incomparable rather than two people.

    ⚠ **It is not a provenance marker and not a classifier** (SPEC §11.4). It
    was both until ``v1:`` was retired, and the parameter survives only for the
    rotation seam. Provenance is ``principal.source`` and pseudonymity is
    ``principal.form``; both ride every derivation mode, which a tag cannot do
    because ``"raw"`` emits none. Every caller in this repo now takes the
    default — a second value here would have to be a new key generation.

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
