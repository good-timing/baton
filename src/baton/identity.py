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

⚠ **A ``scheme`` parameter existed here from 2026-09-11 to 0.8.11 and is GONE**,
together with the tag it selected. Both copies now return the bare digest, so
this is the one change that CLOSED a divergence instead of opening one. The
``issuer`` divergence above still stands.
"""

from __future__ import annotations

import hmac
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

# ⚠ **No tag is prefixed onto a hash, and none may be reintroduced** — not for
# provenance, not for a key generation, not for anything (SPEC §11.4, §13
# 0.8.11). ``hash_principal_id`` returns the bare digest.
#
# Two constants lived here and both are RETIRED. ``VENDOR_HASH_SCHEME = "v1"``
# went at 0.8.10: it carried provenance, which is ``principal.source`` now.
# ``HASH_SCHEME = "h1"`` went at 0.8.11: it named the HMAC key generation, and
# a fact about a value must not ride inside the value — that is the whole rule
# ``principal``'s three members exist to make structural.
#
# ⚠ **What 0.8.11 gave up, stated so nobody rediscovers it as a bug:** nothing
# now records WHICH key produced a digest, so rotating the secret replaces a
# tenant's whole population with no marker anywhere. That is accepted — the
# generation could not have re-joined a person across the boundary anyway, since
# the raw value was never stored and no consumer can match new digests to old.
# If rotation awareness is ever wanted it is an OPTIONAL wire member, never a
# tag on this string.


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
) -> str:
    """HMAC-SHA256 a raw principal into a console-safe, per-tenant ``principal.id``.

    ``tenant_id`` is folded into the HMAC MESSAGE (not just the key) so the same
    principal under two tenants can never collide or be cross-tenant-correlated.
    Returns the BARE lowercase hex digest (e.g. ``"9f2c…"``) — no tag, no
    prefix, no scheme. ⚠ It returned ``"h1:<hex>"`` until 0.8.11; see the
    retired constants above.

    ``issuer`` — the OIDC ``iss`` claim — is folded in the same way when
    supplied, because a ``sub`` is unique only within the provider that minted
    it (RFC 7519 §4.1.2; the ``mcp`` SDK repeats the caveat on
    ``AccessToken.subject``). Two identity providers behind one vendor can hand
    out the same ``sub`` to different people, and without the issuer those two
    people hash to one ``principal.id`` — a silent merge of exactly the kind this
    project keeps finding.

    **The digest has NEVER depended on the tag**, and that is why taking it off
    was a relabel rather than a recomputation: the tag was not part of the HMAC
    message, so the hex for a given ``(tenant_id, principal, issuer)`` is
    byte-identical before and after 0.8.11. Only the three characters in front
    of it disappear.

    ⚠ **SPEC §13 still records that as a VALUE change**, because a consumer
    comparing whole id strings across the upgrade sees one actor become two —
    every hashed principal, not just the vendor-asserted ones ``v1:`` covered.
    Stored events are not rewritten, so both spellings of one digest coexist in
    a collector permanently and §11.4 tells a consumer to strip a leading
    ``h<n>:`` before comparing.

    ⚠ **The facts a tag used to carry now travel as members**, and that is the
    rule to keep: provenance is ``principal.source``, pseudonymity is
    ``principal.form``, and both ride every derivation mode — which a tag could
    not do, since ``"raw"`` mode emits no tag and so dropped its provenance
    entirely. Nothing here encodes a fact about the value into the value.

    ⚠ **``issuer=None`` MUST hash byte-identically to the pre-issuer form**, and
    the append-only message layout below is what guarantees it. Every hash
    baton-proxy and baton-extmcp have produced since 0.5.0 was issuer-less, and
    they share this function's contract as parity mirrors. Issuer-bearing hashes
    are new values that never existed before; nothing needs migrating.

    ⚠ **This rule got STRICTER at 0.8.11, and the reason is the tag's removal.**
    A layout change used to be survivable by cutting a new generation — the tag
    would have said which derivation produced a digest. There is no tag now, so
    two layouts produce two indistinguishable populations of hex with nothing
    anywhere recording the difference, in a column nobody can reverse. **Do not
    change the message layout.** If it ever must change, the marker has to go on
    the wire as a member first, because the value can no longer carry one.
    """
    message = f"{tenant_id}\x00{_canonicalize(raw_principal)}"
    if issuer is not None:
        message += f"\x00{_canonicalize(issuer)}"
    return hmac.new(key, message.encode(), sha256).hexdigest()
