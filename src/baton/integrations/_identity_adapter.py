"""End-user identity off a vendor's hook — resolve the principal, organization
and labels and put them on the wire as the hook stated them, with the capture
settings the caller matches.

Shared by BOTH adapters, beside ``_client_observed.py`` and for the same reason:
a capture signal that lives under one adapter's package is one the other
adapter can silently never call.

**Identity comes from the vendor's ``resolve_principal`` hook and from nowhere
else** (SPEC §11.4): who the person behind a call is, which claim names them,
and whether the value is hashed are the vendor's decisions. A vendor who wants
the token's subject or email passes one of the ready-made hooks in
``oauth_hooks.py``; one who wants anything else writes their own. Every
principal this module emits is ``source: "asserted"``.

Identity never comes from ``client_observed``: that is what a client says it
is, and this is who the vendor says the person is. The client's declared name
is read only to match capture settings (SPEC §2.6).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from itertools import islice
from types import MappingProxyType
from typing import Any, TypeVar

from baton._capture_settings import Caller, CallSettings, CaptureSettings, clean_labels
from baton._white_space import UNSENDABLE, is_blank, usable_text
from baton.events import ClientObserved, OrganizationWire, PrincipalWire
from baton.identity import PRINCIPAL_FORM_RAW, PRINCIPAL_FORMS, Organization, Principal
from baton.integrations._config import PrincipalResolutionContext, ResolvePrincipalHook
from baton.integrations._hooks import run_vendor_hook

#: ``principal.source`` — a vendor's own per-request resolver. The vendor
#: states who this is and nothing in the protocol checked the claim, including
#: when the hook read it off a verified token: the SDK cannot see what the hook
#: did. The only value this SDK emits (SPEC §11.4); ``"attested"`` stays
#: registered for stored events and is produced by nobody.
PRINCIPAL_SOURCE_ASSERTED = "asserted"

#: Cap on ``principal.id``: it is external text copied onto every event of the
#: call, so it gets a bound, the same one ``client_observed.info`` has.
PRINCIPAL_ID_MAX_LEN = 128

#: Cap on ``display_name``, in code points. Over it the name is DROPPED, not
#: truncated: SPEC §11.4 forbids rewriting the value, so both SDKs send the
#: same bytes for one resolver output.
DISPLAY_NAME_MAX_LEN = 128


def wire_display_name(value: object) -> str | None:
    """The resolver's ``display_name`` as it goes on the wire, or ``None``.

    Verbatim when usable. A non-string, a blank (only Unicode
    ``White_Space``), an over-long value, or one holding a lone surrogate or
    U+0000 is dropped ALONE — the rest of the principal is still emitted,
    because a bad display name is no reason to lose a good id.
    """
    return value if usable_text(value, DISPLAY_NAME_MAX_LEN) else None


@dataclass(frozen=True)
class ResolvedCaller:
    """The envelope's ``principal``, ``organization`` and ``labels`` for one
    request, and the capture settings it runs under. Principal and
    organization are independent (SPEC §11.4): either, both or neither."""

    principal: PrincipalWire | None = None
    organization: OrganizationWire | None = None
    labels: tuple[str, ...] = ()
    settings: CallSettings | None = None
    """``None`` means this config's values."""

    def envelope_fields(self) -> dict[str, Any]:
        return {
            "principal": self.principal,
            "organization": self.organization,
            "labels": list(self.labels) or None,
            "capture": self.settings.capture if self.settings is not None else None,
        }

    def result_capture_mode(self, code_value: str) -> str:
        return self._setting("result_capture_mode", code_value)

    def intent_param_mode(self, code_value: str) -> str:
        return self._setting("intent_param_mode", code_value)

    def report_mode(self, code_value: str) -> str:
        return self._setting("report_mode", code_value)

    def _setting(self, name: str, code_value: str) -> str:
        value = None if self.settings is None else getattr(self.settings, name)
        return code_value if value is None else value


NO_CALLER = ResolvedCaller()
_LABELS_READ_MAX = 1000


async def resolve_principal_via_hook(
    hook: ResolvePrincipalHook,
    context: PrincipalResolutionContext,
    *,
    logger: logging.Logger,
) -> Principal | Organization | None:
    """Call a vendor's ``resolve_principal`` hook and normalize its result.

    Never raises. An exception is logged and treated as a miss, so the event
    ships without a ``principal`` exactly as if no hook were configured — ``principal`` is additive analytics and a vendor's own bug in
    their resolver may not fail their tool call (SPEC §11.2 fail-open).

    Accepts sync or async hooks, mirroring ``VendorConfig.scrubber``.

    A hook returning a dict, a bare string, or a namedtuple with the right
    field names is a miss, logged, never a partially-built ``Principal``.
    """
    try:
        result = await run_vendor_hook(hook, context, hook_name="resolve_principal", logger=logger)
    except Exception:
        logger.warning(
            "baton: resolve_principal hook raised; the event ships without a principal",
            exc_info=True,
        )
        return None
    return _usable_caller(result, where="resolve_principal hook returned", logger=logger)


def wire_vendor_caller(value: object, *, logger: logging.Logger) -> ResolvedCaller:
    """What the vendor's code passed as ``principal=`` (the library API), as
    wire objects. It takes what a hook may return. Never raises: a bad value is
    logged and dropped, as from a hook."""
    usable = _usable_caller(value, where="principal= was given", logger=logger)
    return _wire_caller(usable, logger=logger)


def _usable_caller(
    value: object, *, where: str, logger: logging.Logger
) -> Principal | Organization | None:
    if value is None or isinstance(value, (Principal, Organization)):
        return value
    logger.warning(
        "baton: %s %s, not a baton.Principal or baton.Organization — ignoring it; "
        "the event ships without a principal. "
        "Pass Principal(principal_id=...), Organization(organization_id=...) or None.",
        where,
        type(value).__name__,
    )
    return None


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

    **A read-only VIEW**, not the token's own dict: the hook is handed the
    same object the vendor's tool handler reads next, so a hook normalizing
    in place would otherwise rewrite the handler's claims. A proxy, not a
    copy — it blocks the write without copying the claims per call.
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


_Wire = TypeVar("_Wire", PrincipalWire, OrganizationWire)


def _wire_identity(
    wire: type[_Wire],
    value_id: object,
    form: object,
    display_name: object,
    *,
    field: str,
    logger: logging.Logger,
) -> _Wire | None:
    """One resolved identity as its wire object, or ``None``.

    The id is sent as given: not scrubbed, since a scrubber that redacts emails
    would map every user onto one redaction constant and merge them into a
    single actor.
    """
    if not isinstance(value_id, str) or is_blank(value_id):
        # A blank id names nobody, and every such caller would merge into one.
        logger.warning("baton: an empty or non-string %s — ignoring it.", field)
        return None
    if UNSENDABLE.search(value_id):
        logger.warning("baton: a %s holding a lone surrogate or U+0000 — ignoring it.", field)
        return None
    if not isinstance(form, str) or form not in PRINCIPAL_FORMS:
        # A type checker refuses this at the hook or ``principal=``; this is
        # for a caller nobody type-checks. SPEC §11.4: anything not "hashed" is personal data.
        logger.warning(
            "baton: %s with form %r, expected one of %s — sending it as %r.",
            field,
            form,
            sorted(PRINCIPAL_FORMS),
            PRINCIPAL_FORM_RAW,
        )
        form = PRINCIPAL_FORM_RAW
    return wire(
        id=value_id[:PRINCIPAL_ID_MAX_LEN],
        source=PRINCIPAL_SOURCE_ASSERTED,
        form=form,
        display_name=wire_display_name(display_name),
    )


def _wire_principal(principal: Principal, *, logger: logging.Logger) -> PrincipalWire | None:
    return _wire_identity(
        PrincipalWire,
        principal.principal_id,
        principal.form,
        principal.display_name,
        field="principal_id",
        logger=logger,
    )


def _wire_organization(organization: object, *, logger: logging.Logger) -> OrganizationWire | None:
    if organization is None:
        return None
    if not isinstance(organization, Organization):
        logger.warning(
            "baton: a Principal's organization is %s, not a baton.Organization — ignoring it.",
            type(organization).__name__,
        )
        return None
    return _wire_identity(
        OrganizationWire,
        organization.organization_id,
        organization.form,
        organization.display_name,
        field="organization_id",
        logger=logger,
    )


def _wire_caller(
    resolved: Principal | Organization | None, *, logger: logging.Logger
) -> ResolvedCaller:
    """The resolver's answer as wire objects. An unusable principal or
    organization is dropped alone (SPEC §11.4)."""
    if resolved is None:
        return NO_CALLER
    if isinstance(resolved, Organization):
        return ResolvedCaller(
            organization=_wire_organization(resolved, logger=logger),
            labels=_labels_of(resolved, logger=logger),
        )
    organization = resolved.organization
    return ResolvedCaller(
        principal=_wire_principal(resolved, logger=logger),
        organization=_wire_organization(organization, logger=logger),
        labels=_labels_of(resolved, organization, logger=logger),
    )


def _labels_of(*sources: object, logger: logging.Logger) -> tuple[str, ...]:
    labels: list[Any] = []
    for source in sources:
        given = getattr(source, "labels", ())
        if isinstance(given, str | bytes) or not isinstance(given, Iterable):
            continue
        try:
            # Bounded, so an endless iterator from a resolver cannot hang the call.
            labels.extend(islice(given, _LABELS_READ_MAX))
        except Exception:
            logger.exception("baton: reading the resolver's labels failed; sending none from it")
    return clean_labels(labels)


async def resolve_caller(
    *,
    hook: ResolvePrincipalHook | None,
    hook_context: PrincipalResolutionContext | None,
    logger: logging.Logger,
    settings: CaptureSettings | None = None,
    client: ClientObserved | None = None,
) -> ResolvedCaller:
    """The envelope's ``principal``, ``organization`` and labels for one call, and with
    ``settings`` the capture settings it runs under (SPEC §2.6). The identity is the
    vendor's hook, or nothing.

    No principal, organization or labels when no hook is configured or it has no
    answer, which is the common case and never an error. SPEC §11.4 carries the consumer-side rules.
    There is no fallback: a vendor who wants the token's subject passes
    ``baton.principal_from_oauth_sub`` as the hook.

    ⚠ **The hook is not consulted when it is not configured, and that path must
    stay free.** ``hook_context`` is built by the caller only when ``hook`` is
    not ``None`` — the token read is not free on every tool call of every
    server that will never set this field. Headers are read on every call
    regardless, and both adapters share that one read with the hook. A configured
    hook with a ``None`` context is treated as no hook rather than as an error.

    Fail-open throughout, like everything on this path: a hook that raises,
    returns the wrong type, or returns ``None`` costs the event its
    ``principal``, ``organization`` and labels and nothing else, and a failure
    applying settings leaves this config's values.
    """
    if hook is None or hook_context is None:
        caller = NO_CALLER
    else:
        resolved = await resolve_principal_via_hook(hook, hook_context, logger=logger)
        caller = _wire_caller(resolved, logger=logger)
    if settings is None:
        return caller
    try:
        call_settings = settings.for_caller(_match_view(caller, client))
    except Exception:
        logger.exception(
            "baton: applying capture settings failed; this call uses the config's values"
        )
        return caller
    return caller if call_settings is None else replace(caller, settings=call_settings)


def _match_view(caller: ResolvedCaller, client: ClientObserved | None) -> Caller:
    identity = caller.principal or caller.organization
    return Caller(
        labels=caller.labels,
        client_name=client.info.name if client is not None and client.info is not None else None,
        share_key=identity.id if identity is not None else None,
    )
