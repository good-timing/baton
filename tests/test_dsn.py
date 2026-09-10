"""The DSN parser — the contract between the console's mint and this SDK.

The console mints the string; this module is the only thing that reads it. A
parser that is STRICTER than the mint refuses valid keys in the field, which is
why several cases below assert deliberate PERMISSIVENESS rather than rejection.

Two rules this file is built to hold:

1. **No error ever repeats the credential.** A DSN carries a bearer, and an
   exception carrying one lands in tracebacks, log aggregators and pasted
   issue reports. Every raising case asserts the key is absent from the
   message, not merely that a message appeared.
2. **The ingest origin is scheme + authority and nothing else.** The path
   segments are DATA. A parser that folds them into the URL, or that appends
   ``/v0/events`` itself, reproduces a double-append this project has already
   shipped once — so the origin is asserted by equality, never by ``in``.
"""

from __future__ import annotations

import logging

import pytest

from baton._dsn import VENDOR_ID_PATTERN, Dsn, parse_dsn, redact, resolve_dsn

WORKSPACE = "ten_655b084e118b43f88992ee6357fcc23c"
KEY = "baton_pk_" + "a" * 43
DSN = f"https://{KEY}@ingest.goodtiming.ai/{WORKSPACE}/echo-server"


class TestTheHappyPath:
    def test_it_unpacks_all_four_values(self) -> None:
        assert parse_dsn(DSN) == Dsn(
            origin="https://ingest.goodtiming.ai",
            tenant_id=WORKSPACE,
            vendor_id="echo-server",
            key=KEY,
        )

    def test_the_origin_carries_no_path(self) -> None:
        """Asserted by EQUALITY. ``HttpSink`` appends ``/v0/events`` to this
        value, so an origin that kept the workspace and server segments would
        POST to a URL that does not exist — and a substring check would pass
        happily on exactly that bug."""
        assert parse_dsn(DSN).origin == "https://ingest.goodtiming.ai"

    def test_a_trailing_slash_is_ignored(self) -> None:
        assert parse_dsn(DSN + "/") == parse_dsn(DSN)

    def test_http_is_accepted_for_local_development(self) -> None:
        parsed = parse_dsn(f"http://{KEY}@localhost:8000/{WORKSPACE}/echo-server")
        assert parsed.origin == "http://localhost:8000"

    def test_the_port_survives(self) -> None:
        """The authority is taken verbatim rather than rebuilt from
        ``urlsplit``'s ``hostname``, which drops the port and the brackets an
        IPv6 literal needs."""
        assert parse_dsn(f"https://{KEY}@127.0.0.1:9443/{WORKSPACE}/srv").origin == (
            "https://127.0.0.1:9443"
        )

    def test_the_whole_key_is_the_bearer(self) -> None:
        """Prefix included. The auth layer hashes the entire string, so a
        parser that helpfully stripped ``baton_pk_`` would hand the collector a
        value that matches no row."""
        assert parse_dsn(DSN).key == KEY

    def test_the_workspace_keeps_its_prefix_and_its_case(self) -> None:
        """``ten_`` is part of the id. And the value is compared as a string
        server-side, so this parser must never normalise it."""
        mixed = "ten_655B084E118B43F88992EE6357FCC23C"
        assert parse_dsn(f"https://{KEY}@h.example.com/{mixed}/srv").tenant_id == mixed


class TestDeliberatePermissiveness:
    """Looser than the written grammar, on purpose. Being stricter than the
    mint breaks a customer; being looser only fails to catch a typo that the
    collector rejects readably anyway."""

    def test_the_key_tail_is_not_length_checked(self) -> None:
        """The grammar says 43 characters and the mint says so today. That
        number belongs to the console — pinning it here means the day the mint
        changes, every SDK already in the field refuses every new key."""
        for tail_length in (32, 43, 44, 80):
            key = "baton_pk_" + "b" * tail_length
            assert parse_dsn(f"https://{key}@h.example.com/{WORKSPACE}/srv").key == key

    def test_the_server_segment_uses_the_installers_own_validator(self) -> None:
        """Not a second regex restating its ceiling. The DSN's server segment
        IS a ``vendor_id``, and this repo has already costed that ceiling wrong
        twice by writing the number down somewhere else."""
        from baton.integrations import _config

        assert _config._VENDOR_ID_PATTERN is VENDOR_ID_PATTERN

    def test_a_48_character_server_is_accepted_and_49_is_not(self) -> None:
        """The boundary is READ OFF the validator rather than typed in, so this
        test cannot outlive a change to it."""
        longest = "s" * 48
        assert parse_dsn(f"https://{KEY}@h.example.com/{WORKSPACE}/{longest}").vendor_id == longest
        with pytest.raises(ValueError, match="where the server belongs"):
            parse_dsn(f"https://{KEY}@h.example.com/{WORKSPACE}/{'s' * 49}")


class TestWhatItRefuses:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(KEY, "bare key", id="a-bare-publishable-key"),
            pytest.param("baton_sk_" + "c" * 43, "bare key", id="a-bare-secret-key"),
            pytest.param(f"ingest.goodtiming.ai/{WORKSPACE}/srv", "https://", id="no-scheme"),
            pytest.param(
                f"ftp://{KEY}@h.example.com/{WORKSPACE}/srv", "https://", id="wrong-scheme"
            ),
            pytest.param(f"https://h.example.com/{WORKSPACE}/srv", "carries no key", id="no-key"),
            pytest.param(
                f"https://{KEY}:secret@h.example.com/{WORKSPACE}/srv",
                "no password field",
                id="a-password-slot",
            ),
            pytest.param(
                f"https://{KEY}@h.example.com/{WORKSPACE}",
                "exactly two path segments",
                id="no-server-segment",
            ),
            pytest.param(
                f"https://{KEY}@h.example.com/{WORKSPACE}/srv/extra",
                "exactly two path segments",
                id="three-segments",
            ),
            pytest.param(
                f"https://{KEY}@h.example.com/srv/{WORKSPACE}",
                "where the workspace belongs",
                id="segments-the-wrong-way-round",
            ),
            pytest.param(
                f"https://{KEY}@h.example.com/{WORKSPACE}/my.server",
                "where the server belongs",
                id="a-dot-in-the-server-name",
            ),
            pytest.param("", "non-empty", id="empty"),
        ],
    )
    def test_it_raises_with_a_message_that_names_the_problem(self, raw: str, expected: str) -> None:
        with pytest.raises(ValueError, match=expected):
            parse_dsn(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            f"ftp://{KEY}@h.example.com/{WORKSPACE}/srv",
            f"https://{KEY}:secret@h.example.com/{WORKSPACE}/srv",
            f"https://{KEY}@h.example.com/{WORKSPACE}",
            f"https://{KEY}@h.example.com/{WORKSPACE}/srv/extra",
            f"https://{KEY}@h.example.com/srv/{WORKSPACE}",
            f"https://{KEY}@h.example.com/{WORKSPACE}/my.server",
        ],
        ids=[
            "wrong-scheme",
            "password-slot",
            "no-server-segment",
            "three-segments",
            "swapped-segments",
            "bad-server-name",
        ],
    )
    def test_no_refusal_ever_repeats_the_credential(self, raw: str) -> None:
        """The one thing a parse error must not do. Every message above passes
        through ``redact``; this is the test that keeps the next one doing so."""
        with pytest.raises(ValueError) as caught:
            parse_dsn(raw)
        assert KEY not in str(caught.value)
        assert "a" * 43 not in str(caught.value)

    def test_a_bare_key_is_told_where_to_find_the_real_one(self) -> None:
        """The likeliest paste error by far — /account labels the key type
        "Publishable key" and the string you copy "DSN" — so it earns a
        sentence rather than a parse error."""
        with pytest.raises(ValueError, match="/account"):
            parse_dsn(KEY)

    def test_a_bare_key_is_not_echoed_back_either(self) -> None:
        with pytest.raises(ValueError) as caught:
            parse_dsn(KEY)
        assert KEY not in str(caught.value)


class TestASecretKeyWarnsAndWorks:
    """The row is the authority on what a key may do, not its prefix. An SDK
    enforcing a console policy turns a typo at the mint site into a confusing
    client-side error — but a workspace secret inside a server that ships to
    strangers is worth saying out loud, and this is the only place that can."""

    def test_it_still_parses(self) -> None:
        secret = "baton_sk_" + "d" * 43
        parsed = parse_dsn(f"https://{secret}@h.example.com/{WORKSPACE}/srv")
        assert parsed.key == secret

    def test_it_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        secret = "baton_sk_" + "d" * 43
        with caplog.at_level(logging.WARNING, logger="baton._dsn"):
            parse_dsn(f"https://{secret}@h.example.com/{WORKSPACE}/srv")
        assert "baton_sk_" in caplog.text
        assert "publishable" in caplog.text.lower()

    def test_the_warning_does_not_repeat_the_secret(self, caplog: pytest.LogCaptureFixture) -> None:
        """Naming the prefix is the point; naming the key would put a workspace
        secret into whatever ships the vendor's logs."""
        secret = "baton_sk_" + "d" * 43
        with caplog.at_level(logging.WARNING, logger="baton._dsn"):
            parse_dsn(f"https://{secret}@h.example.com/{WORKSPACE}/srv")
        assert secret not in caplog.text
        assert "d" * 43 not in caplog.text

    def test_a_publishable_key_warns_about_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="baton._dsn"):
            parse_dsn(DSN)
        assert caplog.text == ""


class TestRedact:
    def test_it_removes_the_key_and_keeps_everything_useful(self) -> None:
        assert redact(DSN) == f"https://***@ingest.goodtiming.ai/{WORKSPACE}/echo-server"

    def test_a_string_it_cannot_split_becomes_a_marker_not_a_leak(self) -> None:
        """ "I could not parse it" must never turn into "here is your token"."""
        assert redact(KEY) == "<dsn>"
        assert redact("") == "<dsn>"


class TestResolveDsn:
    def test_explicit_beats_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BATON_DSN", "https://env@h/ten_x/srv")
        assert resolve_dsn(DSN) == DSN

    def test_the_environment_is_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BATON_DSN", DSN)
        assert resolve_dsn(None) == DSN

    def test_neither_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BATON_DSN", raising=False)
        assert resolve_dsn(None) is None

    def test_an_empty_environment_variable_is_not_a_dsn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Set-but-empty is how a shell exports a variable it failed to fill.
        Treating it as a value would raise a parse error naming a string the
        vendor never wrote."""
        monkeypatch.setenv("BATON_DSN", "")
        assert resolve_dsn(None) is None
