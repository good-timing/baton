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
import os

import pytest

from baton._dsn import (
    VENDOR_ID_PATTERN,
    Dsn,
    parse_dsn,
    redact,
    resolve_dsn,
    select_dsn,
)

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


class TestTheWorkspaceIsEightOrThirtyTwoHex:
    """The two minted lengths, and the ones either side of each.

    ⚠ **The lengths are TYPED IN, not read off ``_WORKSPACE_PATTERN``.**
    Deriving them would make this pass under any pattern — including the
    ``^ten_[0-9a-fA-F]+$`` that provoked it, which parsed ``ten_a`` and reddened
    nothing in the whole suite. Pinning a boundary means naming it somewhere the
    implementation cannot move.

    The adjacent lengths are the discriminating half: 7/9 and 31/33 are what a
    ``+``, a ``{8,}`` or a ``{8,32}`` would wave through.
    """

    @pytest.mark.parametrize(
        "workspace",
        [
            pytest.param("ten_" + "a" * 8, id="8-hex-what-the-mint-writes-today"),
            pytest.param("ten_" + "a" * 32, id="32-hex-the-shape-it-replaced"),
            pytest.param("ten_" + "A" * 8, id="8-hex-uppercase"),
            pytest.param("ten_" + "A" * 32, id="32-hex-uppercase"),
            pytest.param("ten_7cd4c8cf", id="8-hex-a-real-minted-value"),
        ],
    )
    def test_an_accepted_length_parses_and_is_passed_through_verbatim(self, workspace: str) -> None:
        assert parse_dsn(f"https://{KEY}@h.example.com/{workspace}/srv").tenant_id == workspace

    @pytest.mark.parametrize(
        "length",
        [0, 1, 7, 9, 16, 31, 33, 64],
    )
    def test_every_other_length_is_refused(self, length: int) -> None:
        workspace = "ten_" + "a" * length
        with pytest.raises(ValueError, match="where the workspace belongs"):
            parse_dsn(f"https://{KEY}@h.example.com/{workspace}/srv")

    @pytest.mark.parametrize("workspace", ["ten_" + "g" * 8, "ten_" + "g" * 32])
    def test_a_right_length_run_of_non_hex_is_still_refused(self, workspace: str) -> None:
        """Length alone is not the rule; the alphabet is half of it."""
        with pytest.raises(ValueError, match="where the workspace belongs"):
            parse_dsn(f"https://{KEY}@h.example.com/{workspace}/srv")

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(f"https://{KEY}@h.example.com/{WORKSPACE}", id="no-server-segment"),
            pytest.param(f"https://{KEY}@h.example.com/{KEY}/srv", id="key-in-the-workspace-slot"),
            pytest.param(f"https://x@{KEY}/{WORKSPACE}/srv", id="key-in-the-host-slot"),
        ],
    )
    def test_no_other_refusal_shows_an_example_with_ONE_length_in_it(self, raw: str) -> None:
        """An example DSN in an unrelated refusal must not name a length.

        ⚠ **This regressed once, in the commit that widened the pattern.** The
        three illustrative examples were rewritten 32 -> 8 along with everything
        else, so a pre-2026-09-12 customer with a 32-hex workspace who made some
        OTHER mistake was shown ``/ten_<8 hex>/<server>`` and could 'correct' a
        perfectly good workspace by truncating it. That DSN parses, so the
        install succeeds and every event then 401s at ingest — which
        ``HttpSink`` classifies as a permanent failure and drops without a log
        line. The length belongs in the one sentence that is ABOUT the length;
        everywhere else the segment is elided, exactly as the key already is.
        """
        with pytest.raises(ValueError) as excinfo:
            parse_dsn(raw)
        assert "hex" not in str(excinfo.value), str(excinfo.value)

    def test_the_refusal_names_both_lengths(self) -> None:
        """A sentence naming only one of two accepted shapes sends the reader
        looking for a typo that is not there."""
        with pytest.raises(ValueError, match="8 or 32 hex characters"):
            parse_dsn(f"https://{KEY}@h.example.com/ten_abc/srv")


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
            # Added after the TypeScript port found them. They belong in the
            # PROPERTY test and not only in the class below: every earlier leak
            # of this kind was a shape nobody had thought to parametrize, so
            # the guarantee is worth stating over the widest input list there
            # is rather than case by case.
            f"https://{KEY}",
            f"https://{KEY}/",
            f"https://{KEY}@h.example.com/{WORKSPACE}/srv-{KEY}",
            f"https://{KEY}@h.example.com/tenant-{KEY}/srv",
            f"https://{KEY}@h.example.com\\evil.com/{WORKSPACE}/srv",
            f"https://{KEY}@h.example.com\nevil.com/{WORKSPACE}/srv",
            f"https://{KEY}@h.example.com:notaport/{WORKSPACE}/srv",
            f"https://x@{KEY}/{WORKSPACE}/srv",
            f"https://h.example.com@{KEY}/{WORKSPACE}/srv",
        ],
        ids=[
            "wrong-scheme",
            "password-slot",
            "no-server-segment",
            "three-segments",
            "swapped-segments",
            "bad-server-name",
            "key-in-the-authority",
            "key-in-the-authority-trailing-slash",
            "key-glued-to-the-server",
            "key-glued-to-the-workspace",
            "backslash-in-the-host",
            "line-break-in-the-host",
            "port-that-is-not-a-number",
            "key-in-the-host-slot-behind-userinfo",
            "key-and-host-the-wrong-way-round",
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


class TestTheTwoLeaksReviewFound:
    """Both got past the suite above, and both are the one failure this module
    exists to prevent: a bearer token in an exception. Kept as their own class
    because the lesson is shared — a redaction is only as good as the WORST
    input anyone can hand it, and the cases already written all happened to be
    well-formed."""

    def test_a_second_at_sign_does_not_carry_the_key_through_redact(self) -> None:
        """``redact`` split on the FIRST ``@`` while the parser split on the
        last, so this input put the credential on the safe-looking side of the
        split and the "redacted" message shipped it whole."""
        raw = f"ftp://x@{KEY}@h.example.com/{WORKSPACE}/srv"
        assert KEY not in redact(raw)
        with pytest.raises(ValueError) as caught:
            parse_dsn(raw)
        assert KEY not in str(caught.value)

    def test_a_host_urlsplit_refuses_does_not_leak_the_key(self) -> None:
        """``urlsplit`` raises on a netloc that is not NFKC-safe and puts the
        WHOLE netloc — userinfo included — in its message. That exception used
        to propagate untouched from the line before the first redaction."""
        raw = f"https://{KEY}@h℀.example.com/{WORKSPACE}/srv"
        with pytest.raises(ValueError) as caught:
            parse_dsn(raw)
        assert KEY not in str(caught.value)
        assert "not a parseable URL" in str(caught.value)

    @pytest.mark.parametrize(
        "raw",
        [
            f"https://h.example.com/{WORKSPACE}/{KEY}",
            f"https://h.example.com/{KEY}/srv",
            f"https://{KEY}@h.example.com/{WORKSPACE}/{KEY}",
            f"https://{KEY}@h.example.com/{KEY}/srv",
        ],
        ids=[
            "server-slot",
            "workspace-slot",
            "server-slot-with-a-real-key",
            "workspace-slot-with-a-real-key",
        ],
    )
    def test_a_key_pasted_into_a_PATH_slot_is_not_echoed(self, raw: str) -> None:
        """The third leak of this kind, and the likeliest paste error of all.

        A DSN's userinfo and its two path segments look alike to someone
        copying by eye. Put the key in the path and there is no ``@`` at all,
        so ``redact`` reported "no key" and then printed the path — with the
        key in it — and the pattern-mismatch messages interpolated the segment
        on top of that. Both halves are elided now, and the refusal names the
        SLOT instead of repeating the value.
        """
        assert KEY not in redact(raw)
        with pytest.raises(ValueError) as caught:
            parse_dsn(raw)
        assert KEY not in str(caught.value)
        assert "a" * 43 not in str(caught.value)

    def test_a_misplaced_key_is_told_which_mistake_it_made(self) -> None:
        """Not merely refused. "carries no key" is true and useless when the
        key is right there in the path."""
        with pytest.raises(ValueError, match="the key is in the PATH"):
            parse_dsn(f"https://h.example.com/{WORKSPACE}/{KEY}")

    def test_a_genuinely_absent_key_keeps_the_original_message(self) -> None:
        """The two cases must not collapse into one sentence: a vendor who
        pasted half a DSN and one who pasted it wrong need different advice."""
        with pytest.raises(ValueError, match="the value from /account"):
            parse_dsn(f"https://h.example.com/{WORKSPACE}/srv")

    def test_the_redacted_message_has_no_cause_carrying_the_original(self) -> None:
        """``raise ... from None``, deliberately: chaining would put the very
        string we just redacted back into the traceback under ``__cause__``."""
        raw = f"https://{KEY}@h℀.example.com/{WORKSPACE}/srv"
        with pytest.raises(ValueError) as caught:
            parse_dsn(raw)
        assert caught.value.__cause__ is None
        assert KEY not in repr(caught.value.__context__)


class TestSelectDsn:
    """An environment variable is not something the caller passed.

    Folding the two together let an ambient ``BATON_DSN`` collide with an
    explicit config and then blame the caller for a value that appears nowhere
    in their code — while also inverting this SDK's precedence rule, under
    which explicit wins and the environment is the fallback.
    """

    def test_an_explicit_dsn_beside_an_explicit_value_still_raises(self) -> None:
        with pytest.raises(ValueError, match="already supplies it"):
            select_dsn(DSN, {"vendor_id": True}, "VendorConfig")

    def test_an_explicit_dsn_alone_is_used(self) -> None:
        assert select_dsn(DSN, {"vendor_id": False}, "VendorConfig") == DSN

    def test_an_ambient_dsn_alone_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The hosted-vendor case it exists for: one variable instead of five."""
        monkeypatch.setenv("BATON_DSN", DSN)
        assert select_dsn(None, {"vendor_id": False, "sink": False}, "Client") == DSN

    def test_an_ambient_dsn_LOSES_to_an_explicit_value_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression this fixes: a vendor who exported BATON_DSN for one
        server could not install a second one the old explicit way — the
        install died naming a ``dsn`` they never wrote."""
        monkeypatch.setenv("BATON_DSN", DSN)
        assert select_dsn(None, {"vendor_id": True}, "VendorConfig") is None

    def test_being_ignored_is_announced(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silence here is the shape where broken and unbuilt look alike: a
        healthy install whose events go nowhere near the collector the vendor
        thinks they configured."""
        monkeypatch.setenv("BATON_DSN", DSN)
        with caplog.at_level(logging.WARNING, logger="baton._dsn"):
            select_dsn(None, {"vendor_id": True, "sink": True}, "VendorConfig")
        assert "BATON_DSN" in caplog.text
        assert "IGNORED" in caplog.text
        # It names WHICH values won, so the vendor can act on it.
        assert "sink" in caplog.text and "vendor_id" in caplog.text

    def test_the_announcement_does_not_repeat_the_key(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("BATON_DSN", DSN)
        with caplog.at_level(logging.WARNING, logger="baton._dsn"):
            select_dsn(None, {"vendor_id": True}, "VendorConfig")
        assert KEY not in caplog.text

    def test_nothing_anywhere_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BATON_DSN", raising=False)
        assert select_dsn(None, {"vendor_id": True}, "Client") is None


class TestTheLeaksTheTypeScriptPortFound:
    """Five more, found by reviewing this module while porting it to
    ``baton-ts`` and by ``/code-review`` on that port — every one of them live
    in the published 0.8.1.

    **Each was reproduced in Python before being fixed, not translated.** Two
    of the TypeScript fixes do not carry over: there, WHATWG folds a backslash
    into a path, so the check asserts the parsed authority's ``pathname`` is
    ``/``; here ``urlsplit`` folds nothing and the authority is simply not a
    host, which is a different question with a different answer. And the
    TypeScript object leaked through ``toJSON`` and ``util.inspect``, where
    Python's leaks through a dataclass ``repr``.
    """

    def test_a_key_with_a_scheme_in_front_of_it_is_not_printed(self) -> None:
        """⚠ **The retry this module CAUSES.** A bare key is told to copy the
        full value, "which starts with https://" — so the obvious second
        attempt is that key with a scheme glued on. It has no ``@`` and no
        path, the elision only ever ran on path segments, and the refusal
        printed the whole bearer under a marker claiming there was no key."""
        raw = f"https://{KEY}"
        assert KEY not in redact(raw)
        with pytest.raises(ValueError) as caught:
            parse_dsn(raw)
        assert KEY not in str(caught.value)

    def test_it_is_told_what_is_MISSING_rather_than_that_it_has_no_key(self) -> None:
        """Redacting it is not enough. "carries no key" is false to the reader
        holding the key, and sends someone who has already followed the hint
        once round the same loop."""
        with pytest.raises(ValueError, match="is a key with a scheme in front of it"):
            parse_dsn(f"https://{KEY}")

    @pytest.mark.parametrize(
        "raw",
        [
            f"https://{KEY}@h.example.com/{WORKSPACE}/srv-{KEY}",
            f"https://{KEY}@h.example.com/tenant-{KEY}/srv",
            f"https://{KEY}@h.example.com/{WORKSPACE}/srv-baton_sk_{'b' * 43}",
        ],
        ids=["glued-to-the-server", "glued-to-the-workspace", "glued-secret-key"],
    )
    def test_a_key_GLUED_to_a_path_segment_is_not_printed(self, raw: str) -> None:
        """⚠ **Deterministic, not a near miss.** The elision was prefix-anchored
        and a glued-on key starts with neither prefix — while a segment with a
        key glued to it is over 48 characters, so it ALWAYS fails
        ``VENDOR_ID_PATTERN`` and always reaches the interpolation that prints
        it. The bearer landed in the same sentence as its own redaction."""
        with pytest.raises(ValueError) as caught:
            parse_dsn(raw)
        assert KEY not in str(caught.value)
        assert "b" * 43 not in str(caught.value)

    def test_a_glued_key_is_still_told_which_SLOT_it_is_in(self) -> None:
        """The sweep decides what no sentence may contain; the refusal decides
        which sentence. Without this the vendor gets the pattern-mismatch
        message, which is true and unhelpful when the reason it does not match
        is a credential."""
        with pytest.raises(ValueError, match="has a KEY in the server slot"):
            parse_dsn(f"https://{KEY}@h.example.com/{WORKSPACE}/srv-{KEY}")

    def test_a_DSN_missing_only_its_at_sign_is_not_told_its_path_is_missing(
        self,
    ) -> None:
        """⚠ **The sentence added above was too eager**, and review caught it
        pointing at a complete DSN whose ``@`` was dropped while editing. That
        input has a host and both path segments — visible in the redacted echo
        in the very same sentence — so "what is missing is the rest: an @, the
        host, and the two path segments" is false twice over. The older message
        is the right one here; the new one is for the retry where a bare key
        really is all the vendor has."""
        with pytest.raises(ValueError, match="the value from /account"):
            parse_dsn(f"https://{KEY}h.example.com/{WORKSPACE}/srv")

    def test_the_bare_key_retry_still_gets_the_new_sentence(self) -> None:
        """The gate must not cost the case it was written for."""
        with pytest.raises(ValueError, match="is a key with a scheme in front of it"):
            parse_dsn(f"https://{KEY}")

    def test_the_sweep_does_not_eat_the_modules_own_help_text(self) -> None:
        """The scan runs over finished sentences, and those sentences contain
        ``https://baton_pk_...@host/ten_.../server``. The tail floor is what
        separates an example from a credential — dots are not in the key
        alphabet, so the ellipsis survives."""
        with pytest.raises(ValueError) as caught:
            parse_dsn(f"https://h.example.com/{WORKSPACE}/srv")
        assert "baton_pk_...@host" in str(caught.value)


class TestTheAuthorityMustBeAHost:
    """⚠ **The one place this parser is deliberately STRICT.** Everywhere else
    it is looser than the grammar, on the stated grounds that a collector
    rejects a typo readably — but a bad host is exactly the case where nothing
    ever reaches a collector to do the rejecting. Measured: the request raises
    ``ConnectError``, ``safe_write``'s fail-open boundary logs it, and the
    vendor sees an install that succeeded and events that never arrive.
    """

    def test_a_key_in_the_HOST_slot_is_refused_rather_than_parsed(self) -> None:
        """⚠ **The worst shape of the lot, and it survived the first pass of
        this lane.** With ANY userinfo — one character will do —
        ``rpartition("@")`` puts the key in the authority, so it misses the
        no-``@`` branch that has a sentence for it, and nothing else objects:
        both path segments validate and the DSN PARSES.

        What that produces is not a leaked message but a leaked object.
        ``origin`` becomes ``https://baton_pk_...``, which rides on the config,
        prints through the ``repr`` this lane had just made safe, and is handed
        to ``httpx`` as a hostname — putting the bearer in ``httpcore``'s
        connection trace at DEBUG on every delivery attempt.
        """
        for raw in (
            f"https://x@{KEY}/{WORKSPACE}/srv",
            f"https://h.example.com@{KEY}/{WORKSPACE}/srv",
            f"https://a@{KEY}:8443/{WORKSPACE}/srv",
        ):
            with pytest.raises(ValueError, match="has the KEY where the host belongs"):
                parse_dsn(raw)

    def test_that_refusal_does_not_repeat_the_credential_either(self) -> None:
        with pytest.raises(ValueError) as caught:
            parse_dsn(f"https://x@{KEY}/{WORKSPACE}/srv")
        assert KEY not in str(caught.value)

    def test_a_backslash_in_the_host_is_refused(self) -> None:
        """``urlsplit`` does not fold ``\\`` the way WHATWG does — the whole
        string stays the authority and ``httpx`` keeps it whole as the host."""
        with pytest.raises(ValueError, match="something other than a host"):
            parse_dsn(f"https://{KEY}@ingest.example.com\\evil.com/{WORKSPACE}/srv")

    def test_a_space_in_the_host_is_refused(self) -> None:
        with pytest.raises(ValueError, match="something other than a host"):
            parse_dsn(f"https://{KEY}@ingest.example.com evil.com/{WORKSPACE}/srv")

    def test_a_port_that_is_not_a_number_is_refused(self) -> None:
        """Same silent class: it cannot be dialled, and the only thing that
        would say so is a connection attempt nobody is watching."""
        with pytest.raises(ValueError, match="port that is not a number"):
            parse_dsn(f"https://{KEY}@h.example.com:notaport/{WORKSPACE}/srv")

    @pytest.mark.parametrize(
        "authority",
        [
            "h.example.com",
            "h.example.com:8443",
            "localhost:8000",
            "127.0.0.1:9",
            "[::1]:8000",
            "HOST.Example.COM",
            "xn--caf-dma.example",
            "ünïcode.example",
        ],
        ids=["host", "host-port", "localhost", "ipv4", "ipv6", "uppercase", "punycode", "idn"],
    )
    def test_every_host_that_actually_works_still_parses(self, authority: str) -> None:
        """⚠ **The reason this is a denylist and not a host pattern.** A
        ``[A-Za-z0-9.-]`` allowlist would read as obviously correct and would
        refuse the last two — an IDN host and its punycode form both resolve,
        and a parser stricter than the mint is what breaks a customer. IPv6 and
        the local-development cases are here because ``examples/03_local_https``
        is a real install shape, not a hypothetical."""
        parsed = parse_dsn(f"https://{KEY}@{authority}/{WORKSPACE}/srv")
        assert parsed.origin == f"https://{authority}"


class TestATabOrLineBreakIsDeletedRatherThanRefused:
    """⚠ **The strip is the defect, not the defence.** ``urlsplit`` removes
    ``\\t``, ``\\r`` and ``\\n`` from the URL before splitting it
    (``_UNSAFE_URL_BYTES_TO_REMOVE``), so every check in this module runs on a
    string the vendor did not write — and ``httpx`` strips identically, so
    nothing downstream notices either.

    Refused in ``parse_dsn`` against the RAW value, which is the last place
    that can still see the character. A closed set rather than a sample: every
    other control character and the space survive into the authority, where the
    host check catches them.
    """

    @pytest.mark.parametrize("character", ["\t", "\r", "\n"], ids=["tab", "cr", "lf"])
    def test_a_break_inside_the_host_is_refused_not_silently_joined(self, character: str) -> None:
        """Without this the origin becomes ``https://ingest.example.comevil.com``
        — one host, well-formed, and not one anybody typed."""
        with pytest.raises(ValueError, match="probably a line wrap"):
            parse_dsn(f"https://{KEY}@ingest.example.com{character}evil.com/{WORKSPACE}/srv")

    @pytest.mark.parametrize("character", ["\t", "\r", "\n"], ids=["tab", "cr", "lf"])
    def test_a_break_inside_the_SERVER_segment_is_refused_too(self, character: str) -> None:
        """The worse half, and the reason this is checked over the whole string
        rather than the authority alone: ``srv{c}x`` parses as ``vendor_id``
        ``"srvx"`` — the server the key is BOUND to, rewritten into one nobody
        minted, on events that then carry it."""
        with pytest.raises(ValueError, match="probably a line wrap"):
            parse_dsn(f"https://{KEY}@h.example.com/{WORKSPACE}/srv{character}x")

    def test_a_TRAILING_newline_is_still_just_whitespace(self) -> None:
        """The common case — a value read from a file, or a shell heredoc — and
        it must keep working. ``strip()`` above handles it; only a break INSIDE
        the value is a refusal."""
        assert parse_dsn(f"{DSN}\n").vendor_id == "echo-server"

    def test_the_refusal_does_not_repeat_the_credential(self) -> None:
        with pytest.raises(ValueError) as caught:
            parse_dsn(f"https://{KEY}@h.example.com\nevil.com/{WORKSPACE}/srv")
        assert KEY not in str(caught.value)


class TestTheParsedObjectDoesNotPrintItsBearer:
    """⚠ **A frozen dataclass prints every field.** Four ways a parsed config
    reaches a log line by accident, none of them deliberate — and its
    TypeScript twin needed a different fix (``toJSON`` plus the inspect
    symbol), which is why this was worth reproducing rather than porting.
    """

    def test_repr_omits_the_key(self) -> None:
        assert KEY not in repr(parse_dsn(DSN))

    def test_str_and_f_string_and_percent_s_omit_it_too(self) -> None:
        """All three render through ``__repr__`` for a dataclass, but they are
        the three shapes that actually appear in logging calls, so they are
        asserted rather than reasoned about."""
        parsed = parse_dsn(DSN)
        assert KEY not in str(parsed)
        assert KEY not in f"{parsed}"
        assert KEY not in "%s" % (parsed,)  # noqa: UP031 — the %-format path IS the case

    def test_what_a_reader_still_needs_is_all_there(self) -> None:
        """Omitted, not masked — and everything that helps someone diagnose an
        install stays visible."""
        shown = repr(parse_dsn(DSN))
        assert "ingest.goodtiming.ai" in shown
        assert WORKSPACE in shown
        assert "echo-server" in shown

    def test_reading_the_key_by_name_is_unchanged(self) -> None:
        """``repr=False`` hides it from printing, not from the SDK: the sink is
        built from this field."""
        assert parse_dsn(DSN).key == KEY


class TestAnEmptyDsnIsUnsetRatherThanSupplied:
    """⚠ **``is not None`` was the odd one out**, and ``baton-ts`` diverged
    from it deliberately in ``9cde895`` so the back-port would be found once
    rather than twice. Two functions in this module already map
    ``BATON_DSN=""`` to unset, and every other config value in this SDK treats
    empty as absent.
    """

    def test_an_empty_dsn_beside_an_explicit_value_does_not_raise(self) -> None:
        """The shape that broke: ``dsn=os.environ.get("MY_DSN", "")`` beside a
        ``vendor_id``. The install died naming a dsn the vendor never filled,
        and pointed them at the wrong value to delete."""
        assert select_dsn("", {"vendor_id": True}, "VendorConfig") is None

    def test_an_empty_dsn_falls_through_to_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset means unset, including for precedence — an empty explicit
        value must not shadow the variable the way a real one does."""
        monkeypatch.setenv("BATON_DSN", DSN)
        assert select_dsn("", {"vendor_id": False}, "Client") == DSN
        assert resolve_dsn("") == DSN

    def test_a_real_dsn_beside_an_explicit_value_still_raises(self) -> None:
        """The refusal this must not weaken: both values are in the vendor's
        own source, and picking one would route a server's traffic under
        someone else's identity."""
        with pytest.raises(ValueError, match="already supplies it"):
            select_dsn(DSN, {"vendor_id": True}, "VendorConfig")


def test_the_suite_never_runs_with_an_ambient_baton_variable() -> None:
    """``conftest.py``'s autouse fixture scrubs the whole ``BATON_*``
    namespace, and this is the assertion that says so out loud.

    ⚠ **Written for ``BATON_DSN`` alone first, which is how it missed the other
    five.** Review reproduced it: four exported variables redded five tests,
    two of them in this lane's own parity file. Asserted over the namespace so
    a variable the SDK gains later cannot reopen the gap.

    ⚠ **It can only fail on a machine that has one exported**, which is exactly
    the population it protects: an ambient DSN supplies the vendor id, the
    tenant id AND the sink, so a developer who set one for a real server would
    have this suite building ``HttpSink``s at a live collector and POSTing
    fixture events into a real workspace. The fixture scrubbed
    ``BATON_DISABLED`` only until this lane; a failing test is a nuisance, and
    this was test data in production.
    """
    leaked = sorted(name for name in os.environ if name.startswith("BATON_"))
    assert leaked == []
