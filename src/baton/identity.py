"""Principal identity — what a vendor's resolver hands back.

Baton attaches the resolved principal (``principal``) to every event so the
Console can group by ``(tenant_id, vendor_id, principal.id)``, at whatever grain
the vendor resolved (SPEC §11.4). The resolver decides everything about the
value: who it names, whether it is hashed, what a page shows for it. The SDK
sends it on as stated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

PRINCIPAL_FORM_RAW = "raw"
PRINCIPAL_FORM_HASHED = "hashed"
PRINCIPAL_FORMS = frozenset({PRINCIPAL_FORM_RAW, PRINCIPAL_FORM_HASHED})


@dataclass(frozen=True)
class Principal:
    """A resolved principal — a person, a service account or an organisation,
    whichever the resolver can honestly name.

    ``principal_id``, ``form`` and ``display_name`` reach the wire as given
    (SPEC §11.4). ``user_data`` is what a hook hands back and nothing more —
    no code path reads it.
    """

    principal_id: str
    display_name: str | None = None
    """What a page shows for this principal. The vendor chooses what is safe
    to show; it is personal data whatever ``form`` says."""
    user_data: dict[str, Any] | None = None
    form: Literal["raw", "hashed"] = "raw"
    """What ``principal_id`` is: ``"raw"``, a real identity, or ``"hashed"``,
    a pseudonym the resolver derived itself. A consumer treats anything not
    ``"hashed"`` as personal data, so leave it unset unless the resolver
    hashed the value."""


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
