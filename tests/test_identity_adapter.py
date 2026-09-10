"""``user_id`` resolution — the field, the fail-open matrix, and the traps.

Unit-level. The end-to-end halves live in
``tests/integrations/official/test_user_id.py`` (which ``mcp-matrix`` runs
against the versions where ``claims`` does not exist) and
``tests/functional/test_user_id_parity.py`` (which drives both adapters).

The stub tokens here are deliberately shaped like the real thing rather than
duck-typed dicts: ``_OldBandToken`` reproduces the mcp 1.20/1.25 ``AccessToken``
field set exactly, because "the attribute is missing" is the behaviour this
module's ``getattr`` exists for and a dict would not have it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from baton.identity import hash_user_id
from baton.integrations.identity_adapter import (
    RAW_USER_ID_MAX_LEN,
    USER_ID_MODE_HASHED,
    USER_ID_MODE_RAW,
    principal_from_access_token,
    resolve_user_id,
)

KEY = b"unit-test-key"
TENANT = "tenant-a"


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


def _resolve(token: Any, **kw: Any) -> str | None:
    params: dict[str, Any] = {
        "mode": USER_ID_MODE_HASHED,
        "tenant_id": TENANT,
        "hmac_key": KEY,
        "logger": logging.getLogger("test"),
        "warned": set(),
    }
    params.update(kw)
    return resolve_user_id(token, **params)


# --------------------------------------------------------------------------
# What gets read, and what must never be
# --------------------------------------------------------------------------


def test_the_subject_claim_is_what_is_read() -> None:
    principal = principal_from_access_token(_Token(claims={"sub": "alice", "iss": "https://idp"}))
    assert principal is not None
    assert principal.user_id == "alice"
    assert principal.issuer == "https://idp"


def test_client_id_is_never_the_identity() -> None:
    """``client_id`` names the OAuth APPLICATION, not the person.

    Measured 2026-09-07: identical (``acme-desktop-app``) for two different
    users on every version tested, because ``JWTVerifier`` falls back
    ``client_id ?? azp ?? sub``. Keying on it merges every user of one app —
    the exact bug ``user_id`` exists to resolve — so it must not appear in the
    output even when it is the only identity-shaped field present.
    """
    assert principal_from_access_token(_Token(client_id="acme-desktop-app")) is None

    # And it must not leak in via the claims either: a token whose claims carry
    # a client_id but no sub yields nothing, not the app.
    token = _Token(claims={"client_id": "acme-desktop-app", "azp": "acme-desktop-app"})
    assert principal_from_access_token(token) is None

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
    assert principal_from_access_token(_Token(subject="alice", claims=None)) is None


# --------------------------------------------------------------------------
# The mcp < 1.27 band
# --------------------------------------------------------------------------


def test_a_token_without_claims_degrades_rather_than_raising() -> None:
    """mcp 1.20 / 1.25 have no ``claims`` attribute at all.

    The requirement is that this is a MISS, not a crash: a tool call must not
    fail because the vendor pinned an older mcp (SPEC §11.2 fail-open).
    """
    assert principal_from_access_token(_OldBandToken()) is None
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

    principal = principal_from_access_token(_VendorToken(claims={"sub": "carol"}))
    assert principal is not None
    assert principal.user_id == "carol"


# --------------------------------------------------------------------------
# The issuer fold
# --------------------------------------------------------------------------


def test_two_issuers_with_one_sub_are_two_different_users() -> None:
    """``sub`` is unique only per issuer (RFC 7519 §4.1.2, and the ``mcp``
    SDK's own comment on ``AccessToken.subject``).

    A vendor running two identity providers can hand the same ``sub`` to two
    different people. Without the issuer folded in they hash to one
    ``user_id`` — a silent merge, invisible in every total.
    """
    a = _resolve(_Token(claims={"sub": "alice", "iss": "https://idp-one"}))
    b = _resolve(_Token(claims={"sub": "alice", "iss": "https://idp-two"}))
    assert a != b


def test_issuerless_hashes_are_byte_identical_to_the_pre_issuer_form() -> None:
    """The compatibility guarantee that lets this diverge from baton-proxy.

    Every hash the proxy and extmcp have emitted since 0.5.0 is issuer-less. If
    adding the parameter changed those values, one ``h1:`` tag would name two
    different derivations across the family — precisely what the scheme prefix
    exists to prevent.
    """
    assert hash_user_id("alice", tenant_id=TENANT, key=KEY) == hash_user_id(
        "alice", tenant_id=TENANT, key=KEY, issuer=None
    )


def test_the_tenant_is_still_folded_in() -> None:
    a = _resolve(_Token(claims={"sub": "alice"}), tenant_id="tenant-a")
    b = _resolve(_Token(claims={"sub": "alice"}), tenant_id="tenant-b")
    assert a != b


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def test_hashed_mode_emits_the_scheme_prefix_and_never_the_principal() -> None:
    got = _resolve(_Token(claims={"sub": "alice@acme.com", "iss": "https://idp"}))
    assert got is not None
    assert got.startswith("h1:")
    assert "alice" not in got
    assert "acme.com" not in got
    assert "idp" not in got


def test_raw_mode_emits_the_subject_verbatim() -> None:
    """Verbatim: no canonicalization, no issuer concatenated.

    Both would defeat the only reason to choose this mode — it exists so a
    human can read the value.
    """
    got = _resolve(
        _Token(claims={"sub": "Alice@Acme.COM", "iss": "https://idp"}),
        mode=USER_ID_MODE_RAW,
    )
    assert got == "Alice@Acme.COM"


def test_raw_mode_is_capped() -> None:
    got = _resolve(_Token(claims={"sub": "x" * 500}), mode=USER_ID_MODE_RAW)
    assert got is not None
    assert len(got) == RAW_USER_ID_MAX_LEN


def test_raw_mode_needs_no_hmac_key() -> None:
    """The key is a hashing concern; raw mode does not hash."""
    got = _resolve(_Token(claims={"sub": "alice"}), mode=USER_ID_MODE_RAW, hmac_key=None)
    assert got == "alice"


# --------------------------------------------------------------------------
# Fail-open matrix
# --------------------------------------------------------------------------


def test_no_auth_yields_no_user_id_in_either_mode() -> None:
    """The common case, and every stdio call: MCP auth is ASGI middleware, so
    ``get_access_token()`` returns ``None`` when there is no bearer token."""
    assert _resolve(None) is None
    assert _resolve(None, mode=USER_ID_MODE_RAW) is None


def test_hashed_mode_without_a_key_drops_the_field_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    warned: set[str] = set()
    logger = logging.getLogger("baton.test.identity")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        for _ in range(5):
            got = resolve_user_id(
                _Token(claims={"sub": "alice@acme.com"}),
                mode=USER_ID_MODE_HASHED,
                tenant_id=TENANT,
                hmac_key=None,
                logger=logger,
                warned=warned,
            )
            assert got is None
    hits = [r for r in caplog.records if "HMAC key" in r.message]
    assert len(hits) == 1, f"expected exactly one warning across five calls, got {len(hits)}"
    # The warning explains a silent drop; it must not explain it by printing
    # the identity into the vendor's log files.
    assert "alice" not in hits[0].message
    assert "acme.com" not in hits[0].message


def test_an_empty_or_non_string_subject_is_a_miss() -> None:
    assert principal_from_access_token(_Token(claims={"sub": ""})) is None
    assert principal_from_access_token(_Token(claims={"sub": 12345})) is None
    assert principal_from_access_token(_Token(claims="not-a-dict")) is None


def test_a_hostile_token_object_cannot_fail_a_tool_call() -> None:
    """Fail-open is the SPEC §11.2 contract: identity is additive analytics and
    must never be able to raise into the vendor's handler."""

    class _Exploding:
        @property
        def claims(self) -> dict[str, Any]:
            raise ValueError("boom")

    assert principal_from_access_token(_Exploding()) is None
    assert _resolve(_Exploding()) is None
