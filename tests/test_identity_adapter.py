"""``principal`` resolution through the ready-made OAuth hooks — the field, the
fail-open matrix, and the traps.

These tests pinned the SDK's own token rung until it was deleted; they now run
the same tokens through ``principal_from_oauth_sub`` — the hook that reads the
same claim — so every measured trap (``client_id``, ``subject``, the mcp < 1.27
band) is still pinned, on the code a vendor actually opts into.

Unit-level. The end-to-end halves live in
``tests/integrations/official/test_principal_id.py`` (which ``mcp-matrix`` runs
against the versions where ``claims`` does not exist) and
``tests/functional/test_principal_id_parity.py`` (which drives both adapters).

The stub tokens here are deliberately shaped like the real thing rather than
duck-typed dicts: ``_OldBandToken`` reproduces the mcp 1.20/1.25 ``AccessToken``
field set exactly, because "the attribute is missing" is the behaviour this
module's ``getattr`` exists for and a dict would not have it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from baton import principal_from_oauth_email, principal_from_oauth_sub
from baton.events import PrincipalWire
from baton.identity import Principal
from baton.integrations._config import SessionResolutionContext
from baton.integrations.identity_adapter import (
    PRINCIPAL_ID_MAX_LEN,
    resolve_call_principal,
    token_claims,
)


@dataclass
class _Token:
    """An ``AccessToken`` as it exists on mcp >= 1.27 / fastmcp 2.14+."""

    token: str = "jwt"
    client_id: str = "acme-desktop-app"
    scopes: list[str] = field(default_factory=list)
    subject: str | None = None
    claims: dict[str, Any] | None = None


@dataclass
class _OldBandToken:
    """An ``AccessToken`` as it exists on mcp 1.20 / 1.25 — no ``claims``, no
    ``subject``. Two of the four legs ``mcp-matrix`` runs."""

    token: str = "jwt"
    client_id: str = "acme-desktop-app"
    scopes: list[str] = field(default_factory=list)


def _ctx(token: Any) -> SessionResolutionContext:
    """Built as the adapters build it: the token's claims via ``token_claims``,
    so every token-shape trap below runs through the real extraction."""
    return SessionResolutionContext(
        headers=None, meta=None, tool_name="lookup", arguments={}, claims=token_claims(token)
    )


def _sub(token: Any) -> Principal | None:
    return principal_from_oauth_sub(_ctx(token))


def _resolve(token: Any, hook: Any = principal_from_oauth_sub) -> PrincipalWire | None:
    """The whole call-level path a vendor gets by passing the hook."""
    return asyncio.run(
        resolve_call_principal(
            hook=hook, hook_context=_ctx(token), logger=logging.getLogger("test")
        )
    )


# --------------------------------------------------------------------------
# What gets read, and what must never be
# --------------------------------------------------------------------------


def test_the_subject_claim_is_what_is_read() -> None:
    principal = _sub(_Token(claims={"sub": "alice", "iss": "https://idp"}))
    assert principal is not None
    assert principal == Principal(principal_id="alice")


def test_client_id_is_never_the_identity() -> None:
    """``client_id`` names the OAuth APPLICATION, not the person.

    Measured 2026-09-07: identical (``acme-desktop-app``) for two different
    users on every version tested, because ``JWTVerifier`` falls back
    ``client_id ?? azp ?? sub``. Keying on it merges every user of one app —
    the exact bug ``principal_id`` exists to resolve — so it must not appear in the
    output even when it is the only identity-shaped field present.
    """
    assert _sub(_Token(client_id="acme-desktop-app")) is None

    # And it must not leak in via the claims either: a token whose claims carry
    # a client_id but no sub yields nothing, not the app.
    token = _Token(claims={"client_id": "acme-desktop-app", "azp": "acme-desktop-app"})
    assert _sub(token) is None

    # Two different users of ONE app must not collapse. This is the assertion
    # that would have failed on the naive reading.
    alice = _resolve(_Token(claims={"sub": "alice"}))
    bob = _resolve(_Token(claims={"sub": "bob"}))
    assert alice != bob
    assert alice is not None and bob is not None


def test_subject_is_not_read_even_when_populated() -> None:
    """``subject`` is deliberately not a fallback.

    It is ``None`` for every user across the whole fastmcp 2.x/3.x band (no
    shipped verifier populates it) and is dropped entirely by fastmcp 3.4.2's
    own ``AccessToken`` rebuild — so a shortcut through it is the buggier of
    the two paths while adding a second one to maintain. ``claims["sub"]``
    worked on every combination measured.
    """
    assert _sub(_Token(subject="alice", claims=None)) is None


# --------------------------------------------------------------------------
# The mcp < 1.27 band
# --------------------------------------------------------------------------


def test_a_token_without_claims_degrades_rather_than_raising() -> None:
    """mcp 1.20 / 1.25 have no ``claims`` attribute at all.

    The requirement is that this is a MISS, not a crash: a tool call must not
    fail because the vendor pinned an older mcp (SPEC §11.2 fail-open).
    """
    assert _sub(_OldBandToken()) is None
    assert _resolve(_OldBandToken()) is None


def test_a_vendor_subclass_declaring_claims_is_read_on_any_version() -> None:
    """The escape hatch for the old band, and why the floor was not raised.

    A vendor's ``TokenVerifier`` returns whatever it likes, so one that returns
    an ``AccessToken`` SUBCLASS declaring ``claims`` is read correctly even on
    mcp 1.20 — the ``getattr`` does not care which class defined the field.
    """

    @dataclass
    class _VendorToken(_OldBandToken):
        claims: dict[str, Any] | None = None

    principal = _sub(_VendorToken(claims={"sub": "carol"}))
    assert principal is not None
    assert principal.principal_id == "carol"


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def test_the_id_is_sent_exactly_as_the_hook_returned_it() -> None:
    """No canonicalization, no issuer concatenated, and ``form`` is ``"raw"``
    unless the hook says otherwise."""
    got = _resolve(_Token(claims={"sub": " Alice@Acme.COM", "iss": "https://idp"}))
    assert got == PrincipalWire(id=" Alice@Acme.COM", source="asserted", form="raw")


def _stating(form: Any) -> Any:
    return lambda _ctx: Principal(principal_id="9f2c-vendor-digest", form=form)


def test_a_form_the_hook_states_reaches_the_wire_with_the_id_untouched() -> None:
    got = _resolve(None, hook=_stating("hashed"))
    assert got == PrincipalWire(id="9f2c-vendor-digest", source="asserted", form="hashed")


@pytest.mark.parametrize("form", ["HASHED", "encrypted", "", None, 7, ["hashed"]])
def test_an_unregistered_form_is_sent_as_raw(form: Any, caplog: pytest.LogCaptureFixture) -> None:
    """SPEC §11.4: anything not ``"hashed"`` is personal data."""
    with caplog.at_level(logging.WARNING):
        got = _resolve(None, hook=_stating(form))
    assert got is not None and got.form == "raw" and got.id == "9f2c-vendor-digest"
    assert "form" in caplog.text


def test_the_id_is_capped() -> None:
    got = _resolve(_Token(claims={"sub": "x" * 500}))
    assert got is not None
    assert len(got.id) == PRINCIPAL_ID_MAX_LEN


# --------------------------------------------------------------------------
# Fail-open matrix
# --------------------------------------------------------------------------


def test_no_auth_yields_no_principal() -> None:
    """The common case, and every stdio call: MCP auth is ASGI middleware, so
    ``get_access_token()`` returns ``None`` when there is no bearer token."""
    assert _resolve(None) is None


@pytest.mark.parametrize(
    "blank_sub",
    [pytest.param(" ", id="space"), pytest.param("\t\n", id="tab-newline")],
)
def test_a_whitespace_only_subject_is_a_miss(blank_sub: str) -> None:
    """A truthiness guard passes them, and every such caller would merge into
    one actor naming nobody."""
    assert _sub(_Token(claims={"sub": blank_sub})) is None


def test_an_empty_or_non_string_subject_is_a_miss() -> None:
    assert _sub(_Token(claims={"sub": ""})) is None
    assert _sub(_Token(claims={"sub": 12345})) is None
    assert _sub(_Token(claims="not-a-dict")) is None


def test_a_hostile_token_object_cannot_fail_a_tool_call() -> None:
    """Fail-open is the SPEC §11.2 contract: identity is additive analytics and
    must never be able to raise into the vendor's handler."""

    class _Exploding:
        @property
        def claims(self) -> dict[str, Any]:
            raise ValueError("boom")

    assert _sub(_Exploding()) is None
    assert _resolve(_Exploding()) is None


# --------------------------------------------------------------------------
# Review findings, 2026-09-09 — each of these failed before its fix
# --------------------------------------------------------------------------


def test_a_token_accessor_that_raises_cannot_reach_the_tool_call() -> None:
    """fastmcp's ``get_access_token()`` ends in an explicit ``raise TypeError``
    on its conversion path, reachable when a vendor's verifier returns a
    non-fastmcp ``AccessToken``. It is called while building the hook's
    context, OUTSIDE the hook runner's never-raise boundary."""
    from baton.integrations.standalone import _auth

    def _boom() -> Any:
        raise TypeError("Expected fastmcp.server.auth.auth.AccessToken, got ...")

    original = _auth.get_access_token_or_none
    try:
        _auth.get_access_token_or_none = _boom  # type: ignore[assignment]
        assert _auth.current_access_token() is None
    finally:
        _auth.get_access_token_or_none = original  # type: ignore[assignment]


# --------------------------------------------------------------------------
# ``principal_from_oauth_email``
# --------------------------------------------------------------------------


def _email(token: Any) -> Principal | None:
    return principal_from_oauth_email(_ctx(token))


def test_the_email_hook_keys_on_the_WHOLE_address_and_names_the_local_part() -> None:
    """The local part alone is not an id: ``alice@acme.com`` and
    ``alice@contoso.com`` are two people. It rides as ``display_name``."""
    got = _email(_Token(claims={"email": "alice@acme.com", "sub": "x", "iss": "https://idp"}))
    assert got == Principal(principal_id="alice@acme.com", display_name="alice")


def test_the_same_local_part_at_two_domains_is_two_people() -> None:
    a = _resolve(_Token(claims={"email": "alice@acme.com"}), hook=principal_from_oauth_email)
    b = _resolve(_Token(claims={"email": "alice@contoso.com"}), hook=principal_from_oauth_email)
    assert a is not None and b is not None
    assert a.id != b.id


def test_the_email_hook_does_not_fall_back_to_sub() -> None:
    """No ``email`` claim is a miss, not a cue to read ``sub``: which claim
    names the person is the vendor's choice, and composing the two is one line
    of their own (the module docstring shows it)."""
    assert _email(_Token(claims={"sub": "alice"})) is None


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param(None, id="no-claims"),
        pytest.param({"email": ""}, id="empty"),
        pytest.param({"email": "  "}, id="whitespace"),
        pytest.param({"email": 7}, id="non-string"),
    ],
)
def test_an_unusable_email_is_a_miss(claims: Any) -> None:
    assert _email(_Token(claims=claims)) is None
    assert _email(_OldBandToken()) is None
    assert _email(None) is None


def test_an_address_with_no_at_sign_is_still_the_id_but_has_no_name() -> None:
    """Not every IdP validates the claim's shape. The value is still a stable
    identifier, so it keys the principal; there is just no local part to name."""
    got = _email(_Token(claims={"email": "alice"}))
    assert got == Principal(principal_id="alice", display_name=None)


def test_the_local_part_splits_on_the_LAST_at_sign() -> None:
    """RFC 5321 permits a quoted ``@`` in the local part; the domain never has one."""
    got = _email(_Token(claims={"email": '"a@b"@acme.com'}))
    assert got is not None and got.display_name == '"a@b"'


def test_email_verified_is_not_consulted() -> None:
    """Whether an unverified address is good enough is the vendor's call."""
    got = _email(_Token(claims={"email": "alice@acme.com", "email_verified": False}))
    assert got is not None and got.principal_id == "alice@acme.com"


def test_only_the_shipped_hooks_run_inline_and_nothing_inherits_it() -> None:
    """The fast path keys on IDENTITY. A ``functools.wraps`` wrapper copies a
    function's ``__dict__``, and a ``Mock`` answers truthy for any attribute —
    both took the inline path while it read an attribute (review, 2026-10-02)."""
    import functools
    from unittest.mock import AsyncMock

    from baton.integrations._hooks import _INLINE_HOOKS

    assert any(principal_from_oauth_sub is h for h in _INLINE_HOOKS)
    assert any(principal_from_oauth_email is h for h in _INLINE_HOOKS)

    @functools.wraps(principal_from_oauth_sub)
    def wrapped(ctx: Any) -> Any:
        return principal_from_oauth_sub(ctx)

    def composed(ctx: Any) -> Any:
        return principal_from_oauth_email(ctx) or principal_from_oauth_sub(ctx)

    for vendor_callable in (wrapped, composed, AsyncMock()):
        assert not any(vendor_callable is h for h in _INLINE_HOOKS)


def test_an_unhashable_callable_hook_still_runs() -> None:
    """A callable dataclass is unhashable; the inline check must not raise on it."""
    from dataclasses import dataclass

    @dataclass
    class Resolver:
        def __call__(self, _ctx: Any) -> Principal:
            return Principal(principal_id="u1")

    got = _resolve(None, hook=Resolver())
    assert got is not None and got.source == "asserted"


def test_an_AsyncMock_hook_is_awaited_not_returned_raw() -> None:
    """The observable half of the Mock case: a vendor testing with an
    ``AsyncMock`` hook gets its principal, not an unawaited coroutine."""
    from unittest.mock import AsyncMock

    got = _resolve(None, hook=AsyncMock(return_value=Principal(principal_id="u1")))
    assert got is not None and got.source == "asserted"


def test_the_context_repr_carries_neither_the_claims_nor_the_bearer() -> None:
    """A hook that logs its context must not write out the email or a live
    credential — the standalone adapter delivers ``Authorization`` among the
    headers."""
    ctx = SessionResolutionContext(
        headers={"authorization": "Bearer eyJSECRET"},
        meta=None,
        tool_name="lookup",
        arguments={},
        claims=token_claims(_Token(claims={"email": "alice@acme.com"})),
    )
    text = repr(ctx)
    assert "alice" not in text
    assert "eyJSECRET" not in text


def test_token_claims_is_a_read_only_view() -> None:
    """Shipped hooks run inline on the request; a hook normalizing in place
    must not rewrite the claims the vendor's own handler reads next."""
    original = {"email": "Alice@Acme.com"}
    claims = token_claims(_Token(claims=original))
    assert claims == original
    with pytest.raises(TypeError):
        claims["email"] = "x"  # type: ignore[index]
    assert claims is not original


def test_token_claims_accepts_any_mapping_not_only_dict() -> None:
    """The field is typed ``Mapping``; a verifier storing a ``MappingProxyType``
    must not be silently read as no claims at all."""
    from types import MappingProxyType

    claims = token_claims(_Token(claims=MappingProxyType({"sub": "alice"})))  # type: ignore[arg-type]
    assert claims is not None and claims["sub"] == "alice"


def test_the_hooks_never_raise_on_a_hostile_token() -> None:
    class _Exploding:
        @property
        def claims(self) -> dict[str, Any]:
            raise ValueError("boom")

    assert _email(_Exploding()) is None
    assert _sub(_Exploding()) is None


# --------------------------------------------------------------------------
# display_name on the wire (SPEC §11.4)
# --------------------------------------------------------------------------


def _named(name: Any) -> Any:
    return lambda _ctx: Principal(principal_id="alice", display_name=name)


def test_the_resolver_name_reaches_the_wire() -> None:
    got = _resolve(None, hook=_named("Alice"))
    assert got is not None and got.display_name == "Alice"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param(" \t\n", id="ascii-space"),
        pytest.param("\x85\u3000", id="nel-and-ideographic-space"),
        pytest.param(7, id="non-string"),
        pytest.param("a" * 129, id="over-cap"),
        pytest.param("a\ud800", id="lone-surrogate"),
        pytest.param("jane\x00", id="nul"),
    ],
)
def test_an_unusable_name_is_dropped_and_the_id_is_kept(name: Any) -> None:
    """A bad label costs only itself: the principal still ships."""
    got = _resolve(None, hook=_named(name))
    assert got is not None and got.id and got.display_name is None


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(" Alice ", id="padded"),
        pytest.param("\x1c", id="info-separator"),
        pytest.param("\ufeff", id="bom"),
        pytest.param("a" * 128, id="at-cap"),
        pytest.param("\U0001f600" * 128, id="astral-at-cap"),
    ],
)
def test_a_usable_name_is_sent_verbatim(name: str) -> None:
    """No trimming, and blank is Unicode ``White_Space`` exactly: Python's
    ``strip()`` would drop U+001C, ECMAScript's ``trim()`` would drop U+FEFF,
    and the two SDKs must agree."""
    got = _resolve(None, hook=_named(name))
    assert got is not None and got.display_name == name
