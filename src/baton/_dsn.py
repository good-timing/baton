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
    """
    scheme, _, rest = raw.partition("://")
    if not rest:
        return "<dsn>"
    _, sep, after_key = rest.partition("@")
    if not sep:
        return f"{scheme}://<no key>@{rest}" if scheme else "<dsn>"
    return f"{scheme}://***@{after_key}"


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

    parts = urlsplit(raw)
    safe = redact(raw)

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
