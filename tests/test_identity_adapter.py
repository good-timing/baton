"""``principal_id`` resolution — the field, the fail-open matrix, and the traps.

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

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from baton.identity import hash_principal_id
from baton.integrations.identity_adapter import (
    PRINCIPAL_ID_MODE_HASHED,
    PRINCIPAL_ID_MODE_RAW,
    RAW_PRINCIPAL_ID_MAX_LEN,
    principal_from_access_token,
    resolve_attested_principal,
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
        "mode": PRINCIPAL_ID_MODE_HASHED,
        "tenant_id": TENANT,
        "hmac_key": KEY,
        "logger": logging.getLogger("test"),
        "warned": set(),
    }
    params.update(kw)
    return resolve_attested_principal(token, **params)


# --------------------------------------------------------------------------
# What gets read, and what must never be
# --------------------------------------------------------------------------


def test_the_subject_claim_is_what_is_read() -> None:
    principal = principal_from_access_token(_Token(claims={"sub": "alice", "iss": "https://idp"}))
    assert principal is not None
    assert principal.principal_id == "alice"
    assert principal.issuer == "https://idp"


def test_client_id_is_never_the_identity() -> None:
    """``client_id`` names the OAuth APPLICATION, not the person.

    Measured 2026-09-07: identical (``acme-desktop-app``) for two different
    users on every version tested, because ``JWTVerifier`` falls back
    ``client_id ?? azp ?? sub``. Keying on it merges every user of one app —
    the exact bug ``principal_id`` exists to resolve — so it must not appear in the
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
    assert principal.principal_id == "carol"


# --------------------------------------------------------------------------
# The issuer fold
# --------------------------------------------------------------------------


def test_two_issuers_with_one_sub_are_two_different_users() -> None:
    """``sub`` is unique only per issuer (RFC 7519 §4.1.2, and the ``mcp``
    SDK's own comment on ``AccessToken.subject``).

    A vendor running two identity providers can hand the same ``sub`` to two
    different people. Without the issuer folded in they hash to one
    ``principal_id`` — a silent merge, invisible in every total.
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

    ⚠ **This assertion is self-referential** — both sides come from this
    module, so it proves the default is inert and nothing about agreeing with
    the sibling sensor. The frozen cross-repo vector at the bottom of this file
    is what covers that, and it was added on 2026-09-10 when ``baton-proxy``
    finally took the same ``issuer`` parameter.
    """
    assert hash_principal_id("alice", tenant_id=TENANT, key=KEY) == hash_principal_id(
        "alice", tenant_id=TENANT, key=KEY, issuer=None
    )


def test_the_tenant_is_still_folded_in() -> None:
    a = _resolve(_Token(claims={"sub": "alice"}), tenant_id="tenant-a")
    b = _resolve(_Token(claims={"sub": "alice"}), tenant_id="tenant-b")
    assert a != b


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def test_hashed_mode_emits_the_key_generation_tag_and_never_the_principal() -> None:
    got = _resolve(_Token(claims={"sub": "alice@acme.com", "iss": "https://idp"}))
    assert got is not None
    assert got.id.startswith("h1:")
    assert got.form == "hashed"
    assert got.source == "attested"
    # The whole object, serialised — a member that leaked the subject would
    # pass a check that only read `id`.
    blob = got.model_dump_json()
    assert "alice" not in blob
    assert "acme.com" not in blob
    assert "idp" not in blob


def test_raw_mode_emits_the_subject_verbatim() -> None:
    """Verbatim: no canonicalization, no issuer concatenated.

    Both would defeat the only reason to choose this mode — it exists so a
    human can read the value.
    """
    got = _resolve(
        _Token(claims={"sub": "Alice@Acme.COM", "iss": "https://idp"}),
        mode=PRINCIPAL_ID_MODE_RAW,
    )
    assert got is not None
    assert got.id == "Alice@Acme.COM"
    assert got.form == "raw"
    # ⚠ The provenance SURVIVES raw mode now. It did not while the scheme tag
    # carried it, and that loss is what the object was built to end.
    assert got.source == "attested"


def test_raw_mode_is_capped() -> None:
    got = _resolve(_Token(claims={"sub": "x" * 500}), mode=PRINCIPAL_ID_MODE_RAW)
    assert got is not None
    assert len(got.id) == RAW_PRINCIPAL_ID_MAX_LEN


def test_raw_mode_needs_no_hmac_key() -> None:
    """The key is a hashing concern; raw mode does not hash."""
    got = _resolve(_Token(claims={"sub": "alice"}), mode=PRINCIPAL_ID_MODE_RAW, hmac_key=None)
    assert got is not None and got.id == "alice"


# --------------------------------------------------------------------------
# Fail-open matrix
# --------------------------------------------------------------------------


def test_no_auth_yields_no_principal_id_in_either_mode() -> None:
    """The common case, and every stdio call: MCP auth is ASGI middleware, so
    ``get_access_token()`` returns ``None`` when there is no bearer token."""
    assert _resolve(None) is None
    assert _resolve(None, mode=PRINCIPAL_ID_MODE_RAW) is None


def test_hashed_mode_without_a_key_drops_the_field_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    warned: set[str] = set()
    logger = logging.getLogger("baton.test.identity")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        for _ in range(5):
            got = resolve_attested_principal(
                _Token(claims={"sub": "alice@acme.com"}),
                mode=PRINCIPAL_ID_MODE_HASHED,
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


@pytest.mark.parametrize(
    "blank_sub",
    [pytest.param(" ", id="space"), pytest.param("\t\n", id="tab-newline")],
)
def test_a_whitespace_only_subject_is_a_miss(blank_sub: str) -> None:
    """``hash_principal_id`` canonicalizes NFC → strip → lower, so every
    whitespace-only subject collapses to the SAME digest — measured,
    ``" "`` and ``"\t\n"`` both give ``h1:14fa5f91…``.

    A truthiness guard passes them, and the result is a real, stable
    pseudonym naming nobody that every such caller merges into. Far less
    reachable than on the hook path (a verifier would have to mint one), but
    both paths feed one hash and a guard that differs between them is a guard
    waiting to be copied wrong.
    """
    assert principal_from_access_token(_Token(claims={"sub": blank_sub})) is None


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


# --------------------------------------------------------------------------
# Review findings, 2026-09-09 — each of these failed before its fix
# --------------------------------------------------------------------------


def test_a_str_hmac_key_is_encoded_rather_than_exploding_at_call_time() -> None:
    """``VendorConfig(principal_id_hmac_key="secret")`` must work.

    ``hmac.new`` takes bytes and raises ``TypeError: key: expected bytes`` on a
    ``str`` — and it raises inside the tool call, not at install, so the vendor
    sees it only once a real authenticated request arrives. That is the ONE
    deployment shape they cannot reach in local testing (identity needs HTTP
    plus OAuth), which makes it the worst possible place to fail. The env var
    has always accepted a string, so a vendor moving a working secret out of
    ``BATON_PRINCIPAL_ID_HMAC_KEY`` and into the field hits exactly this.
    """
    from baton.integrations._config import _resolve_principal_id_hmac_key

    assert _resolve_principal_id_hmac_key("secret", mode="hashed") == b"secret"
    assert _resolve_principal_id_hmac_key(b"secret", mode="hashed") == b"secret"
    # And the two spellings must agree, or moving the secret between them
    # would silently re-pseudonymise every user.
    from baton.identity import hash_principal_id

    assert hash_principal_id("alice", tenant_id=TENANT, key=b"secret") == hash_principal_id(
        "alice",
        tenant_id=TENANT,
        key=_resolve_principal_id_hmac_key("secret", mode="hashed") or b"",
    )


@pytest.mark.parametrize(
    ("new_env", "explicit", "mode", "expected", "warns"),
    [
        (None, None, "hashed", None, True),
        ("new-secret", None, "hashed", b"new-secret", False),
        (None, "explicit-secret", "hashed", b"explicit-secret", False),
        (None, None, "raw", None, False),
    ],
    ids=["old-name-only", "new-env-var", "explicit-field", "raw-mode"],
)
def test_the_renamed_hmac_env_var_is_never_read_and_warned_about_only_when_it_matters(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    new_env: str | None,
    explicit: str | None,
    mode: str,
    expected: bytes | None,
    warns: bool,
) -> None:
    """0.8.6 renamed ``BATON_USER_ID_HMAC_KEY`` with no fallback.

    Hashed identity fails open, so a leftover old variable with nothing in its
    place is warned about. Beside a working key, or in raw mode (no key
    needed), it is not. Its value is never used or logged.
    """
    from baton.integrations import _config

    monkeypatch.setenv("BATON_USER_ID_HMAC_KEY", "old-secret-value")
    if new_env:
        monkeypatch.setenv("BATON_PRINCIPAL_ID_HMAC_KEY", new_env)
    with caplog.at_level(logging.WARNING, logger=_config.logger.name):
        assert _config._resolve_principal_id_hmac_key(explicit, mode=mode) == expected
    assert ("BATON_USER_ID_HMAC_KEY" in caplog.text) is warns
    # The part that tells the operator what to do: the name to set instead.
    assert ("BATON_PRINCIPAL_ID_HMAC_KEY" in caplog.text) is warns
    assert "old-secret-value" not in caplog.text


def test_a_token_accessor_that_raises_cannot_reach_the_tool_call() -> None:
    """fastmcp's ``get_access_token()`` ends in an explicit ``raise TypeError``
    on its conversion path, reachable when a vendor's verifier returns a
    non-fastmcp ``AccessToken``. Called in an argument expression it sat
    OUTSIDE ``resolve_attested_principal``'s never-raise boundary."""
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
# The CROSS-REPO vector — the only assertion that can catch a joint drift
# --------------------------------------------------------------------------

# One principal, one tenant, one key, and the two digests they must produce.
# ⚠ These literals are DUPLICATED VERBATIM in the sibling sensor
# (`baton/tests/test_identity_adapter.py` <-> `baton-proxy/tests/test_identity.py`)
# and that duplication is the entire point: `hash_principal_id` is a hand-maintained
# copy across two repos that cannot import each other, and every other test of
# it compares the implementation to ITSELF. The pre-existing
# "issuer=None matches the pre-issuer form" check asserts
# `hash_principal_id(x) == hash_principal_id(x, issuer=None)` — both sides from the same
# module — so a layout change applied to BOTH repos on the same day stays green
# in both while every `h1:` hash ever emitted becomes unreproducible. A frozen
# literal is the only thing that reds for that, because it was computed before
# the change and no edit can move it.
#
# The principal carries a trailing space and mixed case on purpose: canonical-
# isation (NFC, strip, lower) is part of the derivation, so a divergence there
# is a divergence in the hash.
#
# If one of these ever fails, the answer is NOT to update the literal. It means
# the two sensors have stopped agreeing about what `h1:` denotes, and every
# stored `principal_id` was written under the other definition.
_VECTOR_PRINCIPAL = "Alice@Example.COM "
_VECTOR_TENANT = "ten_abc"
_VECTOR_KEY = b"shared-key-bytes"
_VECTOR_ISSUER = "https://idp.example.com"
_VECTOR_ISSUERLESS = "h1:b8556c3cd4564b06af433259553eadee690754318e27ca392deabba8aac7843b"
_VECTOR_WITH_ISSUER = "h1:9fc18f492b9dfe9092acf9d330d710b648d29b4aa131ecf702938df9409f0e78"


def test_the_shared_cross_repo_vector_issuerless() -> None:
    """Frozen 2026-09-10, when the two copies were verified byte-identical."""
    assert (
        hash_principal_id(_VECTOR_PRINCIPAL, tenant_id=_VECTOR_TENANT, key=_VECTOR_KEY)
        == _VECTOR_ISSUERLESS
    )


def test_the_shared_cross_repo_vector_with_an_issuer() -> None:
    """The issuer fold is append-only, so this pins the APPENDED layout too.

    Without it, only the issuer-less half would be nailed down and the two
    repos could still diverge on where the issuer goes — which is the failure
    the docstring warns cannot be hidden behind a compatible default a second
    time.
    """
    assert (
        hash_principal_id(
            _VECTOR_PRINCIPAL,
            tenant_id=_VECTOR_TENANT,
            key=_VECTOR_KEY,
            issuer=_VECTOR_ISSUER,
        )
        == _VECTOR_WITH_ISSUER
    )
