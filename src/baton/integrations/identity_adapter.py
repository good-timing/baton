"""End-user identity off a vendor's hook — resolve the principal and put it on
the wire as the hook stated it.

Shared by BOTH adapters, beside ``runtime_adapter.py`` and for the same reason:
the last capture signal that lived under one adapter's package was the one the
other adapter never called, and it shipped ``unknown`` on every event for two
releases before anybody noticed.

**Identity comes from the vendor's ``resolve_principal`` hook and from nowhere
else** (SPEC §11.4): who the person behind a call is, which claim names them,
and whether the value is hashed are the vendor's decisions. A vendor who wants
the token's subject or email passes one of the ready-made hooks in
``oauth_hooks.py``; one who wants anything else writes their own. Every
principal this module emits is ``source: "asserted"``.

Keep all of it apart from ``agent_runtime``: that is what a client says it is,
and this is who the vendor says the person is. They answer different questions.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from baton.events import PrincipalWire
from baton.identity import PRINCIPAL_FORM_RAW, PRINCIPAL_FORMS, Principal
from baton.integrations._hooks import run_vendor_hook

if TYPE_CHECKING:
    # Type-only: ``_config`` imports ``ResolvePrincipalHook`` from this module
    # at runtime, so a runtime import back would be a cycle.
    from baton.integrations._config import SessionResolutionContext

#: ``principal.source`` — a vendor's own per-request resolver. The vendor
#: states who this is and nothing in the protocol checked the claim, including
#: when the hook read it off a verified token: the SDK cannot see what the hook
#: did. The only value this SDK emits (SPEC §11.4); ``"attested"`` stays
#: registered for stored events and is produced by nobody.
PRINCIPAL_SOURCE_ASSERTED = "asserted"

#: Cap on ``principal.id``: it is external text copied onto every event of the
#: call, so it gets a bound, the same one the declared ``agent_runtime`` has.
PRINCIPAL_ID_MAX_LEN = 128

#: Cap on ``display_name``, in code points. Over it the name is DROPPED, not
#: truncated: SPEC §11.4 forbids rewriting the value, so both SDKs send the
#: same bytes for one resolver output.
DISPLAY_NAME_MAX_LEN = 128

# Unicode ``White_Space``, exactly (SPEC §11.4's blank rule). Not
# ``str.strip()``: that also removes U+001C to U+001F, which ECMAScript's
# ``trim()`` keeps, so the two SDKs would disagree on what is blank.
_WHITE_SPACE = frozenset(
    "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


# A lone surrogate cannot be UTF-8 encoded and U+0000 is refused by Postgres
# text; either would cost a collector the whole EVENT, not the label.
_UNSENDABLE = re.compile("[\x00\ud800-\udfff]")


def wire_display_name(value: object) -> str | None:
    """The resolver's ``display_name`` as it goes on the wire, or ``None``.

    Verbatim when usable. A non-string, a blank (only Unicode
    ``White_Space``), an over-long value, or one holding a lone surrogate or
    U+0000 is dropped ALONE — the rest of the principal is still emitted,
    because a bad label is no reason to lose a good id.
    """
    if not isinstance(value, str) or len(value) > DISPLAY_NAME_MAX_LEN:
        return None
    if all(ch in _WHITE_SPACE for ch in value):
        return None
    if _UNSENDABLE.search(value):
        return None
    return value


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
        # A blank id names nobody, and every such caller would merge into one
        # actor.
        logger.warning(
            "baton: resolve_principal hook returned a Principal with an empty or "
            "non-string principal_id — ignoring it."
        )
        return None
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


def _wire_principal(principal: Principal | None, *, logger: logging.Logger) -> PrincipalWire | None:
    """The resolver's principal as the wire object, or ``None``.

    The id is sent as given: not scrubbed, since a scrubber that redacts emails
    would map every user onto one redaction constant and merge them into a
    single actor.
    """
    if principal is None:
        return None
    if _UNSENDABLE.search(principal.principal_id):
        logger.warning(
            "baton: resolve_principal hook returned a principal_id holding a lone "
            "surrogate or U+0000 — ignoring it; the event ships without a principal."
        )
        return None
    form: str = principal.form
    if not isinstance(form, str) or form not in PRINCIPAL_FORMS:
        # A type checker refuses this at the hook; this is for a hook nobody
        # type-checks. SPEC §11.4: anything not "hashed" is personal data.
        logger.warning(
            "baton: resolve_principal hook returned form %r, expected one of %s — "
            "sending it as %r.",
            form,
            sorted(PRINCIPAL_FORMS),
            PRINCIPAL_FORM_RAW,
        )
        form = PRINCIPAL_FORM_RAW
    return PrincipalWire(
        id=principal.principal_id[:PRINCIPAL_ID_MAX_LEN],
        source=PRINCIPAL_SOURCE_ASSERTED,
        form=form,
        display_name=wire_display_name(principal.display_name),
    )


async def resolve_call_principal(
    *,
    hook: ResolvePrincipalHook | None,
    hook_context: SessionResolutionContext | None,
    logger: logging.Logger,
) -> PrincipalWire | None:
    """The envelope's ``principal`` for one call: the vendor's hook, or nothing.

    ``None`` when no hook is configured or it has no answer, which is the
    common case and never an error. SPEC §11.4 carries the consumer-side rules.
    There is no fallback: a vendor who wants the token's subject passes
    ``baton.principal_from_oauth_sub`` as the hook.

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
    return _wire_principal(principal, logger=logger)
