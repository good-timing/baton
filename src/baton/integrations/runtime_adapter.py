"""Per-runtime ``_meta`` heuristics — detect which agent runtime is calling
the vendor's MCP server.

Shared by BOTH adapters. It used to live under ``standalone/``, which is why
the official adapter never called it and shipped ``agent_runtime: "unknown"``
unconditionally on every event since the package was split.

**Prefer what the client DECLARED over what we can infer.** A client names
itself in its ``initialize`` handshake (``clientInfo``), and that is a
declaration; the key-prefix heuristic below is an inference drawn from an
artifact built for another purpose — ``claudecode/toolUseId`` is a per-CALL
tool-use id, and reading a caller's identity off its namespace prefix works
for one vendor by accident of naming and yields nothing for anyone else. That
is why Claude Desktop, Cursor and everything else reported ``unknown``.

The declaration is readable at TOOL-CALL time on both adapters, with no
``initialize`` hook: the handshake params are cached on the session and
exposed as the public ``ctx.session.client_params``. Measured 2026-09-09
across the whole supported band — mcp 1.20 → 2.0, fastmcp 2.14 → 4.0.

Two carriers, because the protocol is mid-move. Old-spec clients (everything
shipping today — a current Claude Code negotiates ``2025-11-25``) send
``clientInfo`` in the handshake and nowhere else. New-spec clients
(MCP 2026-07-28, which deleted that handshake per SEP-2575) put it on EVERY
request under ``io.modelcontextprotocol/clientInfo``. Both are read, so
neither era is a cliff — and neither is a mechanism this ladder is built on
alone, which is the mistake §3.4 rung 4 already made once.

**There is no ``io.baton/agent_runtime`` override any more — removed
2026-09-09 with the rest of the ``io.baton/*`` keys.** It let a caller assert
its own runtime, but no client anywhere ever set it (checked across all eight
repos) and ``instructions.py`` never told one it existed, so the only discovery
path was reading the spec. Its cost was not hypothetical: **B5 happened because
that one key had two documented spellings and no users to notice the
contradiction.** If a real need appears — the strongest candidate is a gateway
asserting the true agent behind a ``clientInfo`` that names the gateway — bring
it back deliberately, with something that tells clients it exists.

⚠ **Call this on the RAW ``_meta``, before the vendor's scrubber runs.** The
default scrubber is an identity no-op, so detecting from the scrubbed dict
passes every test in this repo and fails only at a vendor whose scrubber
touches meta keys — which is the shape of bug this module was moved here to
stop, not one to reintroduce one layer down.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: The per-request carrier for the client's declared identity, reserved by MCP
#: 2026-07-28. Present in the mcp 2.x library today (``mcp.shared.inbound``
#: names the same constant) and EMPTY in practice, because no shipping client
#: negotiates that revision yet — verified 2026-09-09 against claude-code
#: 2.1.267, which asks for ``2025-11-25`` and carries only ``claudecode/*``
#: and ``progressToken`` on a call. Wired anyway: it costs one dict lookup,
#: and the alternative is discovering the day clients move that our only
#: carrier was the one the spec deleted.
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"

#: Cap on any name the CLIENT supplied. Both declared tiers read arbitrary
#: client text and copy it onto every event of the call, so an unbounded value
#: reaches every ``HttpSink`` payload too. 128 is far above any real client
#: name (``claude-code`` is 11) and far below anything worth shipping. Same
#: posture as ``error_body``, the other untrusted string on this wire.
#:
#: The heuristic's own answer is NOT capped or scrubbed — it is a constant
#: this module owns, and mangling it would be the opposite mistake.
CLIENT_NAME_MAX_LEN = 128


def _clean(name: Any, scrubber: Callable[[Any], Any] | None) -> str | None:
    """Scrub and cap a CLIENT-SUPPLIED name, or ``None`` if it is unusable.

    ``None`` rather than a sentinel so an empty or scrubbed-away value falls
    through to the next tier instead of becoming the reported runtime — a
    vendor scrubber that redacts a name must lose that tier, not the whole
    ladder.
    """
    if not isinstance(name, str) or not name:
        return None
    cleaned: str = name
    if scrubber is not None:
        scrubbed = scrubber(cleaned)
        cleaned = scrubbed if isinstance(scrubbed, str) else str(scrubbed)
    if not cleaned:
        return None
    return cleaned[:CLIENT_NAME_MAX_LEN]


def _client_name_from_meta(meta_dict: dict[str, Any]) -> Any:
    """The declared name off the per-request reserved key (new-spec clients).

    The value is an object with ``name``/``version``; it arrives as a plain
    dict through ``meta_to_dict``, but an adapter handing us a model instead
    must not silently read as absent.
    """
    info = meta_dict.get(CLIENT_INFO_META_KEY)
    if isinstance(info, dict):
        return info.get("name")
    return getattr(info, "name", None)


def _client_name_from_context(context: Any) -> Any:
    """The declared name off the session's cached ``initialize`` params.

    Duck-typed and never raising, like every other context read in this SDK:
    ``ctx.session`` and its neighbours raise ``ValueError`` outside a live
    request — which a plain ``getattr(..., None)`` does NOT swallow, since its
    default only covers ``AttributeError``.

    ⚠ **Both spellings are required.** mcp 1.x names the attribute
    ``clientInfo``; mcp 2.x renamed it to ``client_info`` (the wire aliases are
    unchanged). Reading only one is a silent ``unknown`` across an entire major
    version — measured on fastmcp 4, where asking for the 1.x name on a 2.x
    object returned ``None`` and read exactly like "this client is anonymous".
    That is the shape of the bug this module exists because of; it is not
    getting reintroduced one layer down.
    """
    if context is None:
        return None
    try:
        session = getattr(context, "session", None)
        if session is None:
            return None
        params = getattr(session, "client_params", None)
        if params is None:
            return None
        info = getattr(params, "client_info", None)
        if info is None:
            info = getattr(params, "clientInfo", None)
        if info is None:
            return None
        return getattr(info, "name", None)
    except (AttributeError, ValueError, TypeError):
        return None


def meta_to_dict(meta: Any) -> dict[str, Any] | None:
    """Normalize an MCP ``_meta`` value to a plain dict.

    Accepts a dict, an MCP ``RequestParams.Meta`` model, or ``None``. Uses
    ``by_alias=True`` so namespaced keys like ``claudecode/toolUseId`` survive
    the dump (they are model extras whose JSON form is the alias).
    """
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta
    if hasattr(meta, "model_dump"):
        return meta.model_dump(by_alias=True)  # type: ignore[no-any-return]
    return None


def detect_agent_runtime(
    meta: Any,
    *,
    context: Any = None,
    scrubber: Callable[[Any], Any] | None = None,
) -> str | None:
    """Return the detected agent runtime, or ``None`` if no signal.

    ``meta`` is a dict or an MCP ``RequestParams.Meta`` (it normalizes).
    ``context`` is the adapter's MCP context, read only for the session's
    cached handshake; omitting it costs the declared tier and nothing else.

    Detection precedence — **end-to-end signals before hop-local ones**, and
    declared before inferred among equals. First hit wins:

    1. ``_meta["io.modelcontextprotocol/clientInfo"]`` — declared by the client,
       riding the request itself. New-spec clients only; empty today.
    2. Heuristic on key prefixes (``claudecode/*`` → ``claude-code``). Inferred,
       but also rides the request.
    3. ``context.session.client_params`` — declared, from the ``initialize``
       handshake. **This is the tier that does the work today**: every shipping
       client declares here and nowhere else.
    4. ``None`` — caller substitutes its configured default.

    **Why the heuristic sits ABOVE a real declaration, when B1-R's whole point
    was to prefer declarations.** Because the two answer different questions.
    ``_meta`` travels END-TO-END from the agent — a proxy or gateway forwards it
    verbatim, which is how baton-proxy detects a runtime at all — while
    ``clientInfo`` describes only the IMMEDIATE connection hop. So for "which
    agent runtime is calling", the per-call keys are the more end-to-end signal
    even when one of them is an inference.

    This costs nothing anywhere it does not matter. On a direct connection
    Claude Code declares ``claude-code`` AND sends ``claudecode/*``, so tiers 2
    and 3 agree and the order is unobservable. They can only disagree when
    something sits in between — and there tier 2 names the agent while tier 3
    names the middlebox, so ordering it this way is strictly better rather than
    a trade. Every other client reaches tier 3 untouched, because the heuristic
    fires on exactly one prefix.

    Tiers 1-2 return CLIENT-SUPPLIED text, so both are scrubbed and capped;
    tier 3 returns a constant this module owns and is neither. A tier whose
    value scrubs away to nothing falls through to the next one.

    **On the vendor's ``default_agent_runtime``: it stays a FALLBACK, and it
    now applies far less often.** Detection has always won over it
    (``detect_agent_runtime(...) or default``), which was unobservable while
    detection fired on one prefix only. Tier 3 fires for essentially every
    client, so a vendor who configured a default because they "ship into Claude
    Code" will now see whatever the client actually declared instead — often a
    LIBRARY name like ``mcp`` rather than an agent. That is the right way round:
    the default is for when nothing was observed, and a bare mcp client really
    is not Claude Code, so the vendor's install-time guess was simply wrong on
    that connection. It is a behaviour change for any vendor who set it, and
    the CHANGELOG says so.

    ⚠ **Two things this does NOT claim.** The declared name identifies the
    IMMEDIATE MCP client, which behind a gateway is the gateway rather than the
    agent — measured, both fastmcp in-process probes report ``mcp``, the client
    LIBRARY's name. And it is self-asserted, never attested: a client picks its
    own ``clientInfo``. Attested identity is ``user_id``, a different field on
    a different condition; keep the two claims apart.
    """
    meta_dict = meta_to_dict(meta)

    if meta_dict:
        # Tier 1 — declared, on the request.
        declared = _clean(_client_name_from_meta(meta_dict), scrubber)
        if declared is not None:
            return declared

        # Tier 2 — inferred, on the request. Above tier 3 because `_meta`
        # survives a proxy hop and `clientInfo` does not; see the docstring.
        for key in meta_dict:
            if isinstance(key, str) and key.startswith("claudecode/"):
                return "claude-code"

    # Tier 3 — declared, on the connection. Reached by every client that sends
    # no recognized per-call key, which today is all of them but one.
    return _clean(_client_name_from_context(context), scrubber)
