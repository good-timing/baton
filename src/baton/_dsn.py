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
below goes through ``redact``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

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

    key: str
    """The bearer, whole and unmodified — the auth layer hashes the entire
    string including its prefix, so nothing here may trim it."""


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
    """
    scheme, sep, rest = raw.partition("://")
    if not sep:
        return "<dsn>"
    netloc, slash, path = rest.partition("/")
    _, at, authority = netloc.rpartition("@")
    if not at:
        return f"{scheme}://<no key>@{netloc}{slash}{path}"
    return f"{scheme}://***@{authority}{slash}{path}"


def resolve_dsn(explicit: str | None) -> str | None:
    """``dsn``: explicit argument → ``BATON_DSN`` → ``None``.

    The SDK's existing precedence rule, unchanged. ``BATON_DSN`` exists for the
    hosted vendor who will not put the value in source: one environment
    variable instead of five, and their edit rather than a branch in the
    install recipe.
    """
    if explicit is not None:
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

    if explicit is not None:
        if conflicts:
            raise ValueError(
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


def parse_dsn(raw: str) -> Dsn:
    """Unpack a DSN, raising ``ValueError`` on anything that is not one.

    Loud at install, which is the behaviour ``os.environ["X"]`` already has and
    the reason the TypeScript recipe grew an ``env()`` helper: a config value
    that arrives wrong must fail where the vendor is looking, not at the first
    tool call in production.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("dsn must be a non-empty string")

    raw = raw.strip()

    if raw.startswith((_PUBLISHABLE_PREFIX, _SECRET_PREFIX)):
        # No redact() — a bare key has no structure to show, and echoing it is
        # the thing redact() exists to prevent.
        raise ValueError(f"dsn is not a URL: {_BARE_KEY_HINT}")

    safe = redact(raw)
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
        raise ValueError(f"dsn {safe} is not a parseable URL ({reason})")

    if parts.scheme not in ("https", "http"):
        raise ValueError(
            f"dsn {safe} must start with https:// (or http:// for local "
            f"development) — {_BARE_KEY_HINT}"
        )

    key, at, authority = parts.netloc.rpartition("@")
    if not at or not key:
        raise ValueError(
            f"dsn {safe} carries no key — the value from /account has the key "
            f"before an @, as in https://baton_pk_...@host/ten_.../server"
        )
    if ":" in key:
        raise ValueError(
            f"dsn {safe} has a ':' in its key — a DSN carries one credential and no password field"
        )
    if not authority:
        raise ValueError(f"dsn {safe} has no host")

    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 2:
        raise ValueError(
            f"dsn {safe} must carry exactly two path segments — the workspace "
            f"and the server, as in /ten_<32 hex>/<server>. A missing server "
            f"is never defaulted: it is what the key is bound to."
        )
    workspace, server = segments

    if not _WORKSPACE_PATTERN.match(workspace):
        raise ValueError(
            f"dsn {safe} has {workspace!r} where the workspace belongs — "
            f"expected ten_ followed by 32 hex characters. If the two path "
            f"segments are the right way round, this is not a Baton DSN."
        )
    if not VENDOR_ID_PATTERN.match(server):
        raise ValueError(
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
