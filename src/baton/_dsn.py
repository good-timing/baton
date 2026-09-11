"""The packed connection string a wrapped server ships with — parse only.

A **DSN** carries the four values an install needs in one string::

    https://baton_pk_<random>@ingest.goodtiming.ai/ten_<32 hex>/echo-server
    │       │                 │                    │            │
    scheme  key (the bearer)  authority            workspace    server

    dsn        = scheme "://" key "@" authority "/" workspace "/" server
    scheme     = "https" | "http"
    key        = "baton_pk_" tail          ; "baton_sk_" warns and still works
    authority  = host [ ":" port ]
    workspace  = "ten_" 32(hexdigit)
    server     = the vendor_id pattern below

It replaces five environment variables with one value that can sit inline in a
distributable server's source, which is the whole point: a stdio server runs on
every user's machine, so a key that cannot ship means events that never arrive.

**Nothing here reaches the wire.** The envelope still carries ``tenant_id``,
``vendor_id`` and ``consent_token`` as separate fields — the SDK unpacks the
string and fills them in. This is config ergonomics; ingest, SPEC and the
``baton-spec`` vectors are untouched.

**The path segments are DATA, not a route.** Nobody can ``GET`` that URL. The
ingest origin is scheme + authority and *nothing else*; ``HttpSink`` appends
``/v0/events`` itself, exactly as it does for an explicitly-constructed sink.
A parser that appends anything here reproduces a double-append this project has
already shipped once.

**Deliberately more permissive than the grammar in two places**, because a
parser that is STRICTER than the mint is what breaks a customer, while one that
is looser only fails to catch a typo the collector will reject readably anyway:

- **The key's tail is not length-checked.** The grammar says 43 characters and
  the mint says so today; that number belongs to the console, and pinning it
  here means the day the mint changes, every shipped SDK refuses every new key.
  The prefix carries the meaning — length does not.
- **The server segment is checked with ``VENDOR_ID_PATTERN``**, the same object
  ``_validate_vendor_config`` uses, rather than a second regex restating its
  ceiling. That ceiling has already been costed wrong twice on this recipe.

**Never put a parsed DSN in an error message.** It holds a bearer token, and an
exception carrying one lands in tracebacks, logs and issue reports. Every raise
below goes through ``_fail``, which sweeps the finished sentence — a guarantee
that lives at the call sites is one the next message added does not know about.

⚠ **The parsed object does not print its bearer either.** ``Dsn.key`` is
``repr=False``, so ``repr()``, ``str()``, an f-string and ``"%s" % dsn`` — the
four ways an object reaches a log line by accident — show the origin, the
workspace and the server and omit the credential. Reading ``dsn.key`` is
unchanged; so is ``dataclasses.asdict``, which is explicit structural access
rather than an accidental print.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import NoReturn
from urllib.parse import SplitResult, urlsplit

logger = logging.getLogger(__name__)

__all__ = ["VENDOR_ID_PATTERN", "Dsn", "parse_dsn", "redact", "resolve_dsn"]

# Vendor IDs become annotation tool name prefixes; same client-pattern as
# annotation tool names. Reject dots so the default tool name is valid.
#
# It lives HERE rather than in ``integrations/_config.py`` — which is where it
# was defined and where ``_validate_vendor_config`` still applies it — for one
# structural reason: the DSN's server segment IS a vendor_id, ``client.py``
# needs the parser too, and top-level modules must not import from
# ``integrations``. One object, imported both places, so the two rules cannot
# drift apart the way a copied regex would.
VENDOR_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")

# The console mints lowercase, but hex case is not meaningful and refusing an
# uppercase digit would be a rule stricter than the mint. Matched loosely,
# passed through VERBATIM — the value is compared as a string server-side, so
# this parser must never normalise it.
_WORKSPACE_PATTERN = re.compile(r"^ten_[0-9a-fA-F]{32}$")

_PUBLISHABLE_PREFIX = "baton_pk_"
_SECRET_PREFIX = "baton_sk_"


# A credential ANYWHERE in a string, not merely at its start. The tail is
# deliberately unvalidated (see the module docstring), so the alphabet is wide
# and the floor is what keeps this from eating prose: eight is far below any
# real key — the mint writes 43 URL-safe base64 characters — and far above the
# ``...`` in this module's own ``https://baton_pk_...@host/...`` examples,
# whose dots are not in the class. Kept byte-identical to ``baton-ts``'s
# ``KEY_RUN`` so the two parsers cannot drift on what counts as a key.
_KEY_RUN = re.compile(r"baton_(?:pk|sk)_[A-Za-z0-9_%-]{8,}")


def _sweep(message: str) -> str:
    """Every credential in a finished string, replaced by ``<key>``.

    Elided ENTIRELY rather than truncated: a truncated secret is still a
    secret's prefix, and the reader does not need any of it to see what went
    wrong — the slot it landed in is the whole message. Everything around it
    stays readable, which is the point of redacting rather than refusing to say
    anything.
    """
    return _KEY_RUN.sub("<key>", message)


def _fail(message: str) -> NoReturn:
    """The only way this module raises, so the scan cannot be skipped.

    ⚠ **Two raises used to skip it, under a docstring already claiming they
    could not** — the two pattern-mismatch refusals, which interpolate the
    offending segment past a prefix-anchored elision a glued-on key walks
    straight through.

    ⚠ **This sweep is the ONLY redaction, and the numbers say so** — measured
    by mutation on this repo rather than carried over from ``baton-ts``, where
    the same experiment came out differently:

    ========================================  ==========================
    mutation                                  what reddens
    ========================================  ==========================
    ``_sweep`` returns its argument           15 leak tests
    ``_contains_key`` back to ``startswith``  1 SENTENCE test, no leak
    both                                      16
    the key-in-authority branch removed       1 SENTENCE test, no leak
    the key-in-HOST-slot refusal removed      4 — two of them because the
                                              DSN PARSES rather than raises
    ========================================  ==========================

    The asymmetry is structural: ``redact`` has no per-slot elision left, so
    every message that embeds ``safe`` depends on this one function. In
    ``baton-ts`` the sweep and the key-in-slot refusal cover the glued-key
    input jointly and neither is load-bearing alone, and copying that sentence
    here would have described a module this one is not.

    What the refusals DO earn is the sentence a vendor reads — which slot the
    key landed in, or that they have a key and not a DSN — and each is pinned
    by its own test. This decides what no sentence may contain.
    """
    raise ValueError(_sweep(message))


def _contains_key(value: str) -> bool:
    """Whether a credential is anywhere INSIDE this value.

    ⚠ **``startswith`` was the blind spot, and it was load-bearing in three
    places** — the two path-slot refusals and, once review found it, the host
    slot, where a credential does not merely print but parses. A
    segment with a key glued on — ``srv-baton_pk_...``, the shape a paste into
    a half-filled field makes — starts with neither prefix, so it walked past
    both the key-in-slot refusal that names the slot and the "the key is in the
    PATH" hint, landing instead on a pattern-mismatch message that interpolates
    the segment. Reproduced in Python before being fixed: the bearer printed in
    plaintext in the same sentence as its own redaction, and deterministically
    rather than by luck — a segment with a key glued on is over 48 characters,
    so it always fails ``VENDOR_ID_PATTERN`` and always reaches that
    interpolation.

    Expressed through the same scan that redacts, so there is ONE notion of
    "looks like a credential" and it cannot drift from the one that hides it.
    """
    return _KEY_RUN.search(value) is not None


# What a bare key gets told. It is the single most likely paste error — the
# /account page labels the key type "Publishable key" and the string you copy
# "DSN" — so it earns a real sentence rather than a parse error.


_BARE_KEY_HINT = (
    "this looks like a bare key, not a DSN — copy the full value from "
    "/account, which starts with https:// and ends with your server's name"
)


@dataclass(frozen=True)
class Dsn:
    """The four values unpacked from one string. See the module docstring."""

    origin: str
    """Scheme + authority, no path and no trailing slash — what ``HttpSink``
    takes as its base URL and appends ``/v0/events`` to."""

    tenant_id: str
    """The workspace segment, ``ten_`` prefix included and case as minted. The
    prefix is part of the id, not a marker to strip."""

    vendor_id: str
    """The server segment. This is the server the key is BOUND to: a mismatch
    between it and the events' ``vendor_id`` is refused at ingest."""

    key: str = field(repr=False)
    """The bearer, whole and unmodified — the auth layer hashes the entire
    string including its prefix, so nothing here may trim it.

    ⚠ **``repr=False``, and that is the whole fix for a measured leak.** A
    frozen dataclass prints every field, so ``repr(dsn)``, ``str(dsn)``,
    ``f"{dsn}"`` and ``logger.info("%s", dsn)`` each wrote a publishable key
    out — four paths a parsed config reaches a log line by, none of them
    deliberate. The field is omitted rather than masked: ``dsn.key`` still
    returns it, and ``dataclasses.asdict`` still contains it, because both are
    someone asking for the credential by name."""


def redact(raw: str) -> str:
    """A DSN with its credential removed, safe for an error message or a log.

    Keeps everything that helps someone fix the problem (scheme, host,
    workspace, server) and drops the one part that must not be repeated.
    Falls back to a bare marker if the string is too malformed to split, since
    "I could not parse it" must never become "here is your token".

    ⚠ **It splits where the PARSER splits — the LAST ``@`` of the authority,
    and only inside the authority.** The first cut partitioned the whole string
    on its first ``@`` while ``parse_dsn`` used ``rpartition`` on the netloc, so
    a string with two ``@``s in it put the credential on the right-hand side of
    the split and the "redacted" message carried the whole key. Found by review,
    and the existing tests could not see it: every case had exactly one ``@``.
    Two functions splitting one string two ways is the defect, so this one is
    written to mirror the parser rather than to look reasonable on its own.

    ⚠ **A key in the AUTHORITY slot was printed WHOLE, and that case is not
    exotic — it is the retry this module STEERS people into.** The elision ran
    on path segments, and ``https://baton_pk_...`` has none, so the credential
    landed in the netloc and came back out under a ``<no key>`` marker saying
    it was absent. A vendor who pastes a bare key is told "copy the full value
    from /account, which starts with https://"; the obvious next move is to
    prepend ``https://`` to the key already in hand and run it again, putting
    the bearer in the boot log on the second try.

    So the guarantee stopped being a list of slots that each remember to elide
    and became one scan of the finished string: a slot added later cannot
    forget.
    """
    scheme, sep, rest = raw.partition("://")
    if not sep:
        return "<dsn>"
    netloc, slash, path = rest.partition("/")
    _, at, authority = netloc.rpartition("@")
    if not at:
        # ⚠ ``<no key>`` is a statement about the SHAPE — nothing sat before an
        # ``@`` — and not a promise that the string holds no credential. The
        # sweep below is what makes the return safe; when the key is in the
        # netloc this line is exactly the one that used to print it.
        assembled = f"{scheme}://<no key>@{netloc}{slash}{path}"
    else:
        assembled = f"{scheme}://***@{authority}{slash}{path}"
    return _sweep(assembled)


def resolve_dsn(explicit: str | None) -> str | None:
    """``dsn``: explicit argument → ``BATON_DSN`` → ``None``.

    The SDK's existing precedence rule, unchanged. ``BATON_DSN`` exists for the
    hosted vendor who will not put the value in source: one environment
    variable instead of five, and their edit rather than a branch in the
    install recipe.
    """
    if explicit:
        return explicit
    from_env = os.environ.get("BATON_DSN")
    return from_env or None


def select_dsn(explicit: str | None, supplied: dict[str, bool], door: str) -> str | None:
    """Which DSN applies, given what the caller ALSO configured by hand.

    ⚠ **An environment variable is not something the caller passed, and the
    first cut of this treated the two as one value.** ``resolve_dsn`` folded
    ``BATON_DSN`` in before the conflict check ran, so a vendor who exported it
    for one server could not install a SECOND server the old explicit way in the
    same process: the install died accusing them of passing a ``dsn`` that
    appears nowhere in their code. Found by review. It also contradicted the
    precedence rule this SDK states everywhere else — explicit wins, the
    environment is the fallback — by letting an ambient value beat an explicit
    one and then blaming the caller for the collision.

    So the two sources are separated:

    - **An explicit DSN beside an explicit ``vendor_id`` / ``tenant_id`` /
      ``sink`` still raises.** Both are in the vendor's own source; picking one
      would be a guess, and a wrong guess routes a server's traffic under
      someone else's identity.
    - **An ambient ``BATON_DSN`` loses to explicit configuration and is
      ignored**, which is just "explicit wins" applied to a value the caller
      did not write. It still outranks ``BATON_VENDOR_ID`` and friends, which
      is the re-install case the DSN exists for: environment against
      environment, the packed one is the one someone chose today.
    - **Being ignored is announced.** A vendor who exported ``BATON_DSN``
      expecting it to configure this server would otherwise get a healthy
      install that ships events nowhere near the collector — broken and
      unbuilt looking alike, at the one boundary where nobody is watching.
    """
    conflicts = sorted(name for name, was_set in supplied.items() if was_set)

    # ⚠ **Falsy means unset, and ``is not None`` was the odd one out.** Two
    # functions above map ``BATON_DSN=""`` to unset, and every other config
    # value in this SDK treats empty as absent — but an explicitly empty
    # ``dsn`` counted as supplied, so ``VendorConfig(dsn=os.environ.get("X",
    # ""), vendor_id="acme")``, or any loader that fills unset keys with
    # ``""``, died at install naming a dsn the vendor never filled and pointing
    # them at the wrong value to delete. Fixed in ``baton-ts`` first
    # (``9cde895``), which recorded the divergence for this back-port rather
    # than leaving it to be found twice.
    if explicit:
        if conflicts:
            _fail(
                f"{door} got both a dsn and an explicit {conflicts[0]} — the "
                f"dsn already supplies it. Drop one: the dsn is the single "
                f"value from /account, and {conflicts[0]} is what it unpacks "
                f"to."
            )
        return explicit

    ambient = os.environ.get("BATON_DSN") or None
    if ambient is None:
        return None
    if conflicts:
        logger.warning(
            "baton: BATON_DSN is set, but this %s supplies %s directly, so "
            "BATON_DSN is being IGNORED and these events are NOT going to the "
            "collector it names. Remove the explicit value to use it, or unset "
            "BATON_DSN if it was meant for a different server.",
            door,
            ", ".join(conflicts),
        )
        return None
    return ambient


# Characters that cannot appear in a host or a port and that ``urlsplit``
# nonetheless leaves sitting in the authority: the backslash and every kind of
# space — ``\s`` over a ``str`` pattern covers the Unicode ones too.
#
# ⚠ ``\t``, ``\r`` and ``\n`` are absent, and NOT because they are harmless.
# CPython deletes those three before splitting, so by the time an authority
# reaches this pattern they are gone from it — which is the defect, not the
# defence. They are refused in ``parse_dsn``, against the raw string, while
# they can still be seen.
_NOT_IN_A_HOST = re.compile(r"[\\\s\x00-\x1f\x7f]")


def _reject_a_non_host(authority: str, parts: SplitResult, safe: str) -> None:
    r"""Refuse an authority that is not a host, however well-formed the DSN is.

    ⚠ **A BACKSLASH parses, and events then go nowhere.** Measured in Python
    rather than ported: ``urlsplit`` does NOT fold ``\`` into a path the way
    WHATWG does, so ``ingest.example.com\evil`` survives whole as the
    authority, ``httpx`` keeps it whole as the HOST, and the first tool call
    raises ``ConnectError: nodename nor servname provided``. That raise is
    caught by ``safe_write``'s fail-open boundary and logged, so what the
    vendor sees is an install that succeeded and a collector that never
    receives anything — the exact outcome "loud at install" exists to prevent.

    ⚠ **The TypeScript twin's check does not port.** There the fix asserts the
    parsed authority's ``pathname === "/"``, because WHATWG had smuggled a path
    INTO the origin. Nothing is smuggled here: the authority is simply not a
    host, and asking whether anything but a host survived is vacuous when the
    splitter never puts a path there. So the question this asks is the Python
    one — does the authority contain something a host cannot?

    A DENYLIST, not a host pattern, and the asymmetry is deliberate. This
    module is loose elsewhere on the stated grounds that a collector rejects a
    typo readably; that argument inverts here, because a bad host is precisely
    the case where nothing ever reaches a collector to do the rejecting.
    Refusing only what can never be a host keeps an IDN or a punycode label
    working, which a ``[A-Za-z0-9.-]`` pattern would not.
    """
    # ⚠ **A key in the HOST slot PARSES, and that is worse than printing one.**
    # Found by review of the fix above, reproduced first:
    # ``https://x@<key>/ten_.../srv`` splits on the LAST ``@``, so the key lands
    # in the authority and any userinfo at all — one character will do — keeps
    # it out of the no-``@`` branch that has the sentence for this. Nothing
    # downstream objects: the segments validate, ``origin`` becomes
    # ``https://baton_pk_...`` and rides on the config, prints through a
    # ``repr`` this lane had just made safe, and is handed to ``httpx`` as a
    # HOSTNAME — which puts the bearer in ``httpcore``'s connection trace at
    # DEBUG on every attempt.
    #
    # So the slot refusal that guards the workspace and the server guards this
    # one too. It is the same paste error one field to the left: the userinfo
    # and the host sit either side of a single character.
    if _contains_key(authority):
        _fail(
            f"dsn {safe} has the KEY where the host belongs. The order is key, "
            f"@, host — check whether the two are the wrong way round: "
            f"https://baton_pk_...@host/ten_<32 hex>/<server>"
        )
    if _NOT_IN_A_HOST.search(authority):
        _fail(
            f"dsn {safe} has something other than a host between its key and "
            f"its path. The ingest origin is the scheme and the authority and "
            f"nothing else — the workspace and the server are the two path "
            f"segments after it: https://baton_pk_...@host/ten_.../server"
        )
    try:
        parts.port  # noqa: B018 — the ACCESS is the check; SplitResult parses lazily
    except ValueError:
        # Same silent class as the backslash: a port that is not a number
        # cannot be dialled, and the only place that would say so is a
        # connection attempt behind a fail-open boundary.
        _fail(
            f"dsn {safe} has a port that is not a number — the authority is a "
            f"host and an optional numeric port, as in host:8443"
        )


def parse_dsn(raw: str) -> Dsn:
    """Unpack a DSN, raising ``ValueError`` on anything that is not one.

    Loud at install, which is the behaviour ``os.environ["X"]`` already has and
    the reason the TypeScript recipe grew an ``env()`` helper: a config value
    that arrives wrong must fail where the vendor is looking, not at the first
    tool call in production.
    """
    if not isinstance(raw, str) or not raw.strip():
        _fail("dsn must be a non-empty string")

    raw = raw.strip()

    if raw.startswith((_PUBLISHABLE_PREFIX, _SECRET_PREFIX)):
        # No redact() — a bare key has no structure to show, and echoing it is
        # the thing redact() exists to prevent.
        _fail(f"dsn is not a URL: {_BARE_KEY_HINT}")

    safe = redact(raw)

    # ⚠ **A TAB, CR or LF anywhere in the string is REMOVED by ``urlsplit``,
    # not rejected by it** — ``_UNSAFE_URL_BYTES_TO_REMOVE``, measured here
    # rather than read off the TypeScript twin, where WHATWG strips the same
    # three. So the value that gets parsed is not the value the vendor wrote,
    # and every check below runs on the cleaned-up version:
    #
    #     https://<key>@ingest.example.com\nevil.com/ten_.../srv
    #         → origin https://ingest.example.comevil.com — one host, accepted
    #     https://<key>@h.example.com/ten_.../srv\nx
    #         → vendor_id "srvx" — the server the key is BOUND to, silently
    #           rewritten into one nobody minted
    #
    # ``httpx`` strips identically, so nothing downstream notices either: the
    # install succeeds and the events go to a host the vendor never typed, or
    # under a server name they never chose. Refused here because this is the
    # last place that can still SEE the character. It is a closed set, not a
    # sample — every other control character and the space survive into the
    # authority, where ``_reject_a_non_host`` catches them.
    #
    # The surrounding ``strip()`` above already handled the common case, a
    # trailing newline from a file or a shell; what is left is one INSIDE the
    # value, which is a line wrap where it was copied.
    for character, name in (("\t", "tab"), ("\r", "carriage return"), ("\n", "line break")):
        if character in raw:
            _fail(
                f"dsn {safe!r} has a {name} inside it — probably a line wrap "
                f"where the value was copied. It cannot be ignored: a URL "
                f"parser DELETES these rather than refusing them, so the host "
                f"and the server name that would be used are not the ones "
                f"written here. Paste the value from /account as one line."
            )
    # ⚠ ``urlsplit`` raises on a netloc that is not NFKC-safe — an IDN host, or
    # one full-width character in a pasted string — and CPython puts the WHOLE
    # netloc in the message, userinfo included. That exception would propagate
    # untouched, carrying the bearer into a traceback: the single thing this
    # module exists to prevent, arriving through the one line that runs before
    # any of our own checks.
    #
    # ⚠ **Re-raised OUTSIDE the except block, and that placement is the fix.**
    # ``raise ... from None`` was the obvious form and it is not enough: it
    # sets ``__suppress_context__`` so the default traceback printer stays
    # quiet, but ``__context__`` still holds the original exception with the
    # key in it, reachable by anything that walks the chain — an error reporter,
    # a structured logger, pytest's own repr. Raising after the handler has
    # finished leaves no context at all. Only the exception's TYPE NAME crosses
    # over; it carries no input.
    parts = None
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        reason = type(exc).__name__
    if parts is None:
        _fail(f"dsn {safe} is not a parseable URL ({reason})")

    if parts.scheme not in ("https", "http"):
        _fail(
            f"dsn {safe} must start with https:// (or http:// for local "
            f"development) — {_BARE_KEY_HINT}"
        )

    key, at, authority = parts.netloc.rpartition("@")
    if not at or not key:
        # The likeliest way to arrive here is not a missing key but a
        # MISPLACED one: the two path segments and the userinfo all look alike
        # to someone copying by eye. Saying which mistake it is costs one
        # scan and saves the reader the guess.
        # ⚠ **The key in the AUTHORITY slot gets its own sentence, because it
        # is the one mistake this module CAUSES.** A bare key is told to copy
        # the full value, "which starts with https://" — so the next attempt is
        # very often that same key with a scheme glued on front, which has no
        # ``@`` and no path and used to be told, wrongly, that it carried no
        # key at all. Naming what is missing beats repeating the generic hint
        # to someone who has already followed it once.
        # ⚠ **Gated on there being no path, and the gate is the accuracy.**
        # Without it the branch also caught a COMPLETE DSN whose ``@`` was
        # dropped while editing — telling the vendor the host and both path
        # segments were "missing" in a sentence that visibly echoed all three.
        # The pre-existing message is the right one for that input; this one is
        # for the retry where a bare key really is all they have. Found by
        # review of this branch, reproduced before fixing.
        if _contains_key(parts.netloc) and not parts.path.strip("/"):
            _fail(
                f"dsn {safe} is a key with a scheme in front of it, not a DSN — "
                f"what is missing is the rest: an @, the host, and the two path "
                f"segments, as in https://baton_pk_...@host/ten_.../server. The "
                f"whole value is on /account; the key alone is only its first part."
            )
        misplaced = any(_contains_key(segment) for segment in parts.path.split("/"))
        detail = (
            "the key is in the PATH — it goes before an @"
            if misplaced
            else "the value from /account has the key before an @"
        )
        _fail(
            f"dsn {safe} carries no key: {detail}, as in https://baton_pk_...@host/ten_.../server"
        )
    if ":" in key:
        _fail(
            f"dsn {safe} has a ':' in its key — a DSN carries one credential and no password field"
        )
    if not authority or not parts.hostname:
        _fail(f"dsn {safe} has no host")
    _reject_a_non_host(authority, parts, safe)

    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 2:
        _fail(
            f"dsn {safe} must carry exactly two path segments — the workspace "
            f"and the server, as in /ten_<32 hex>/<server>. A missing server "
            f"is never defaulted: it is what the key is bound to."
        )
    workspace, server = segments

    for slot, segment in (("workspace", workspace), ("server", server)):
        if _contains_key(segment):
            # Checked BEFORE the pattern tests below, which would otherwise
            # interpolate the segment — and a key is far more useful to name by
            # its slot than to print back.
            _fail(
                f"dsn {safe} has a KEY in the {slot} slot. The key goes before "
                f"the @, and the path carries the workspace and the server: "
                f"https://baton_pk_...@host/ten_<32 hex>/<server>"
            )

    if not _WORKSPACE_PATTERN.match(workspace):
        _fail(
            f"dsn {safe} has {workspace!r} where the workspace belongs — "
            f"expected ten_ followed by 32 hex characters. If the two path "
            f"segments are the right way round, this is not a Baton DSN."
        )
    if not VENDOR_ID_PATTERN.match(server):
        _fail(
            f"dsn {safe} has {server!r} where the server belongs — it must "
            f"match {VENDOR_ID_PATTERN.pattern!r}, because this value becomes "
            f"the annotation tool name prefix as well as the envelope's "
            f"vendor_id."
        )

    if key.startswith(_SECRET_PREFIX):
        # A warning, never a refusal. The KEY ROW is the authority on what a
        # key may do, not its prefix — an SDK enforcing a console policy turns
        # a typo at the mint site into a confusing client-side error. But a
        # secret key inside a server that ships to strangers is worth saying
        # out loud, and this is the only place that can say it.
        #
        # stderr via logging, never print(): stdout is the JSON-RPC stream
        # under stdio transport, which is exactly the deployment this feature
        # exists for. The prefix only — the point is not to repeat the secret.
        logger.warning(
            "baton: this DSN carries a %s key (a workspace SECRET) where a %s "
            "key belongs. It will work. But if this server ships to anyone "
            "else, that key reads and writes everything the workspace holds — "
            "mint a publishable key at /account instead.",
            _SECRET_PREFIX,
            _PUBLISHABLE_PREFIX,
        )

    return Dsn(
        origin=f"{parts.scheme}://{authority}",
        tenant_id=workspace,
        vendor_id=server,
        key=key,
    )
