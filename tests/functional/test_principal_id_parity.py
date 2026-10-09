"""Both MCP adapters must produce the SAME ``principal`` from the same token,
read through the same ready-made hook.

⚠ Since the SDK's own token rung was deleted, nothing reads the token unless a
hook does, so every token test here passes ``principal_from_oauth_sub`` — and
one test pins that a token with NO hook yields nothing on either adapter.

The companion to ``test_client_observed_parity.py``, and it exists for the same
reason: each adapter's own suite asserts only about itself, so a signal one
adapter silently never resolves is invisible to both. ``identity_adapter.py``
is shared so that cannot happen, and this file is what pins it.

It follows the same two rules as that parity test:

1. **Assert the EXPECTED value, not merely that the two agree.** Two adapters
   broken identically — both returning ``None``, which is precisely the state
   before this change — pass an agreement-only check.
2. **Fail when nothing was checked.** A filter matching no events would
   otherwise turn a vacuous pass into a green tick.

The token is injected per adapter at its own ``_auth`` module, which is also
the assertion: two DIFFERENT auth seams (``mcp.server.auth.middleware`` and
``fastmcp.server.dependencies``) feeding one shared resolver must land on one
value. If a future change reads the token differently on one side, this is
where it shows up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from baton import principal_from_oauth_email, principal_from_oauth_sub
from baton.events import PrincipalWire
from tests._asgi import fake_http_request, starlette_headers
from tests._event_helpers import principal_of, without_surface_snapshots

pytestmark = pytest.mark.functional

TENANT = "tenant-parity"
CLAIMS = {"sub": "alice@acme.example", "iss": "https://idp.example"}


def _read_events(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _official_token(claims: dict[str, Any] = CLAIMS) -> Any:
    from mcp.server.auth.provider import AccessToken

    if "claims" not in AccessToken.model_fields:
        pytest.skip("mcp < 1.27 cannot carry claims; parity is asserted on 1.27+ only")
    return AccessToken(token="jwt", client_id="acme-app", scopes=[], claims=claims)


def _standalone_token(claims: dict[str, Any] = CLAIMS) -> Any:
    from fastmcp.server.dependencies import AccessToken

    return AccessToken(token="jwt", client_id="acme-app", scopes=[], claims=claims)


async def _run_official_path(
    events_path: Path,
    token: Any,
    monkeypatch: pytest.MonkeyPatch,
    resolve_principal: Any = None,
) -> str:
    from baton.integrations.official import VendorConfig, _auth, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink
    from tests._mcp_session import connected_session

    monkeypatch.setattr(_auth, "get_access_token_or_none", lambda: token)
    mcp = FastMCP("parity-official-uid")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="parity",
            vendor_display_name="Parity Vendor",
            consent_token="ct_parity",
            sink=FileSink(str(events_path)),
            tenant_id=TENANT,
            resolve_principal=resolve_principal,
        ),
    )
    try:
        async with connected_session(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
            await client.call_tool(
                handle.annotation_tool_name, {"user_goal": "look up", "signal_type": "failure"}
            )
        return handle.annotation_tool_name
    finally:
        await handle.aclose()


async def _run_standalone_path(
    events_path: Path,
    token: Any,
    monkeypatch: pytest.MonkeyPatch,
    resolve_principal: Any = None,
) -> None:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, _auth, install_baton
    from baton.sinks import FileSink

    monkeypatch.setattr(_auth, "get_access_token_or_none", lambda: token)
    mcp: Any = FastMCP("parity-standalone-uid")

    @mcp.tool
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="parity",
            vendor_display_name="Parity Vendor",
            consent_token="ct_parity",
            sink=FileSink(str(events_path)),
            tenant_id=TENANT,
            resolve_principal=resolve_principal,
        ),
    )
    try:
        async with Client(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
            await client.call_tool(
                handle.annotation_tool_name, {"user_goal": "look up", "signal_type": "failure"}
            )
    finally:
        await handle.aclose()


def _principal_ids(path: Path) -> set[str | None]:
    """Just the ``id`` member, for the tests whose subject is the VALUE."""
    return {p.id if p is not None else None for p in _principals(path)}


def _principals(path: Path) -> set[PrincipalWire | None]:
    """Every event's whole ``principal``, PARSED.

    Parsing rather than comparing dicts is the point: ``PrincipalWire`` is
    ``extra="forbid"`` with three required members, so every event of every
    run here is checked for conformance on the way into the set — a partial
    or over-full object fails before any assertion reads it. It is ``frozen``,
    which is what lets these go in a set at all.

    ``None`` for an event that carried none — which SPEC §11.4 makes the
    common case and never an error. The set is over ALL events of the run for
    the reason recorded at the hook section below: a change wired into the
    tool-call path but not the annotation path yields two entries here.
    """
    events = without_surface_snapshots(_read_events(path))
    assert events, f"no events captured at {path} — the assertion would be vacuous"
    return {
        None if (p := principal_of(ev)) is None else PrincipalWire.model_validate(p)
        for ev in events
    }


def _one_principal(path: Path) -> dict[str, Any]:
    """The single principal a run emitted, as a dict. Fails when a run emitted
    more than one — which is the merge every test here is written to catch."""
    got = _principals(path)
    assert len(got) == 1, f"{path.name} emitted {len(got)} distinct principals: {got}"
    one = got.pop()
    assert one is not None, f"{path.name} emitted no principal at all"
    return one.model_dump()


async def test_both_adapters_send_one_principal_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expected value is stated here rather than found by comparing the two
    runs, because two adapters that both dropped the field agree perfectly."""
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(
        official_path,
        _official_token(),
        monkeypatch,
        resolve_principal=principal_from_oauth_sub,
    )
    await _run_standalone_path(
        standalone_path,
        _standalone_token(),
        monkeypatch,
        resolve_principal=principal_from_oauth_sub,
    )

    assert _principal_ids(official_path) == {CLAIMS["sub"]}
    assert _principal_ids(standalone_path) == {CLAIMS["sub"]}


async def test_both_adapters_drop_the_WHOLE_object_when_unauthenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control, and it is about the object rather than the id.

    SPEC §11.4 makes ``principal`` all-or-nothing: a producer emits all three
    members or omits it. So "nobody was resolved" is a missing object, never an
    object with a null ``id`` and a ``source`` for an identity that does not
    exist — which is malformed, not a degraded reading.
    """
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, monkeypatch)
    await _run_standalone_path(standalone_path, None, monkeypatch)

    assert _principals(official_path) == {None}
    assert _principals(standalone_path) == {None}


async def test_a_verified_token_with_NO_hook_yields_no_principal_on_either_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity is the vendor's choice, so a usable token on its own puts
    nothing on the wire."""
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), monkeypatch)
    await _run_standalone_path(standalone_path, _standalone_token(), monkeypatch)

    assert _principals(official_path) == {None}
    assert _principals(standalone_path) == {None}


async def test_the_email_hook_reads_the_token_on_both_adapters_and_both_emit_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SessionResolutionContext.claims`` is filled by four call sites
    (two adapters x tool call and annotation). ``_one_principal`` collapses
    every event of a run to one value, so a site that forgot the token yields
    a second, ``None``, entry and fails here."""
    claims = {"sub": "opaque-123", "email": "alice@acme.example", "iss": CLAIMS["iss"]}
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(
        official_path,
        _official_token(claims),
        monkeypatch,
        resolve_principal=principal_from_oauth_email,
    )
    await _run_standalone_path(
        standalone_path,
        _standalone_token(claims),
        monkeypatch,
        resolve_principal=principal_from_oauth_email,
    )
    for path in (official_path, standalone_path):
        assert _one_principal(path) == {
            "id": "alice@acme.example",
            "source": "asserted",
            "form": "raw",
            "display_name": "alice",
        }, path.name


# ---------------------------------------------------------------------------
# The vendor identity hook.
#
# These run through the same two drivers as everything above, which is the
# point: each driver calls a tool AND the annotation tool, and ``_principal_ids``
# collapses every emitted event to a SET. A hook wired into the tool-call path
# but not the annotation path yields two values on one run and fails here,
# without a test that names the annotation path at all.

HOOK_SUB = "employee-4417"


def _hook(principal: Any) -> Any:
    """A vendor resolver returning a fixed principal, ignoring the context."""

    def resolve(_ctx: Any) -> Any:
        return principal

    return resolve


async def test_the_hook_supplies_identity_where_no_token_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The headline case: stdio, no token, identity anyway.**

    ``token=None`` is what ``get_access_token()`` returns on every stdio call
    on every supported version — MCP auth is ASGI middleware and stdio has no
    ASGI.
    """
    from baton.identity import Principal

    hook = _hook(Principal(principal_id=HOOK_SUB))
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, monkeypatch, resolve_principal=hook)
    await _run_standalone_path(standalone_path, None, monkeypatch, resolve_principal=hook)

    for path in (official_path, standalone_path):
        assert _one_principal(path) == {
            "id": HOOK_SUB,
            "source": "asserted",
            "form": "raw",
            "display_name": None,
        }, path.name


@pytest.mark.parametrize(
    ("stated", "on_the_wire"),
    [
        pytest.param({"form": "hashed"}, "hashed", id="hashed-is-passed-through"),
        pytest.param({}, "raw", id="unstated-is-raw"),
        pytest.param({"form": "encrypted"}, "raw", id="unregistered-is-raw"),
    ],
)
async def test_the_form_the_hook_states_is_the_form_on_the_wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stated: dict[str, str],
    on_the_wire: str,
) -> None:
    """On both adapters and both emit paths, with the id untouched: the SDK
    never derives ``form``, and never rewrites the value to match it."""
    from baton.identity import Principal

    hook = _hook(Principal(principal_id="9F2C-Vendor-Digest", **stated))
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, monkeypatch, resolve_principal=hook)
    await _run_standalone_path(standalone_path, None, monkeypatch, resolve_principal=hook)

    for path in (official_path, standalone_path):
        assert _one_principal(path) == {
            "id": "9F2C-Vendor-Digest",
            "source": "asserted",
            "form": on_the_wire,
            "display_name": None,
        }, path.name


async def test_a_hook_that_ignores_the_token_is_never_overridden_by_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token here is the SAME one every other test in this file uses, so a
    producer still reading it behind the hook would emit ``CLAIMS["sub"]``
    somewhere on these runs."""
    from baton.identity import Principal

    hook = _hook(Principal(principal_id=HOOK_SUB))
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), monkeypatch, resolve_principal=hook)
    await _run_standalone_path(
        standalone_path, _standalone_token(), monkeypatch, resolve_principal=hook
    )

    for path in (official_path, standalone_path):
        assert _principal_ids(path) == {HOOK_SUB}, path.name


async def test_a_hook_that_raises_costs_the_principal_and_events_still_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vendor's bug in their own resolver may not cost them their capture.

    ``_principals`` fails on a run with no events, so ``{None}`` here means the
    events shipped without a principal — and that the usable token beside the
    broken hook was NOT substituted for it.
    """

    def boom(_ctx: Any) -> Any:
        raise RuntimeError("the vendor's directory service is down")

    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), monkeypatch, resolve_principal=boom)
    await _run_standalone_path(
        standalone_path, _standalone_token(), monkeypatch, resolve_principal=boom
    )

    assert _principals(official_path) == {None}
    assert _principals(standalone_path) == {None}


async def test_an_async_hook_works_on_both_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync or async, matching ``scrubber``. A
    vendor resolving identity will usually be doing I/O to do it."""
    from baton.identity import Principal

    async def resolve(_ctx: Any) -> Any:
        return Principal(principal_id=HOOK_SUB)

    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, monkeypatch, resolve_principal=resolve)
    await _run_standalone_path(standalone_path, None, monkeypatch, resolve_principal=resolve)

    assert _principal_ids(official_path) == {HOOK_SUB}
    assert _principal_ids(standalone_path) == {HOOK_SUB}


async def test_the_hook_sees_the_calls_own_context_not_an_install_time_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook is invoked per request, so it can answer differently per call
    — asserted by returning a principal derived from the context it was
    handed, and then finding BOTH on the wire."""
    from baton.identity import Principal

    def per_call(ctx: Any) -> Any:
        # ``tool_name`` differs between the lookup call and the annotation
        # call, so one hook yields two principals on one run.
        return Principal(principal_id=f"user-of-{ctx.tool_name}")

    official_path = tmp_path / "official.jsonl"
    annotate = await _run_official_path(
        official_path, None, monkeypatch, resolve_principal=per_call
    )
    # The client lists the tools once, and a listing names no tool.
    assert _principal_ids(official_path) == {
        "user-of-lookup",
        f"user-of-{annotate}",
        "user-of-None",
    }


async def test_source_stays_asserted_in_every_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §11.4: ``form`` says nothing about trust and ``source`` says nothing
    about privacy. Neither the stated form nor the KIND of hook (a fixed value,
    or the token read) moves ``source``."""
    from baton.identity import Principal

    resolvers = {
        "token": (_official_token(), principal_from_oauth_sub, "raw"),
        "fixed-raw": (None, _hook(Principal(principal_id=HOOK_SUB)), "raw"),
        "fixed-hashed": (None, _hook(Principal(principal_id=HOOK_SUB, form="hashed")), "hashed"),
    }
    for label, (token, resolver, form) in resolvers.items():
        path = tmp_path / f"{label}.jsonl"
        await _run_official_path(path, token, monkeypatch, resolve_principal=resolver)
        got = _one_principal(path)
        assert got["source"] == "asserted", got
        assert got["form"] == form, got


# ---------------------------------------------------------------------------
# Register A8 — the parity this file exists for, on the one field that did not
# have it.
#
# ``SessionResolutionContext``'s docstring promises a vendor "one hook works
# unmodified regardless of which adapter a vendor is on". For ``headers`` that
# was false from the day it was written: official delivered a case-insensitive
# Starlette ``Headers``, standalone a lowercased plain ``dict``, and the same
# hook resolved 4/4 on one and 0/4 on the other across all six supported
# resolves. Both satisfy the declared ``Mapping[str, str]``, so mypy could not
# see it; each adapter's own suite asserted only about itself, so no test could
# either. That is this file's founding failure mode, recurring on a new field.
#
# So the assertion is not "both are case-insensitive" stated twice. It is ONE
# vendor hook, run against what each adapter actually extracts, reaching the
# same principal — which is the sentence the docstring makes.
# ---------------------------------------------------------------------------

#: Written in canonical case ON PURPOSE. The shared fixture lowercases it the
#: way ASGI does, so this table cannot quietly hand a test a spelling the wire
#: would never deliver — which an earlier draft of these helpers did, by
#: encoding the key unchanged and relying on it having been pre-folded here.
WIRE_HEADERS = {"X-Forwarded-User": "employee-4417", "Authorization": "Bearer t"}

#: What a vendor writes, because it is what the header is called everywhere it
#: is documented. The mismatch between this line and the one above IS A8.
CANONICAL_SPELLING = "X-Forwarded-User"


def _vendor_hook(ctx: Any) -> Any:
    from baton.identity import Principal

    assert ctx.headers is not None
    return Principal(principal_id=ctx.headers[CANONICAL_SPELLING])


def _official_headers() -> Any:
    """What the official adapter hands a hook, via its real extractor."""
    from baton.integrations.official._tool_wrap import _extract_headers_from_context

    class _Ctx:
        headers = starlette_headers(WIRE_HEADERS)

    return _extract_headers_from_context(_Ctx())


def _standalone_headers() -> Any:
    """What the standalone adapter hands a hook, via its real extractor."""
    from fastmcp.server.http import set_http_request

    from baton.integrations.standalone._session import extract_headers

    with set_http_request(fake_http_request(WIRE_HEADERS)):
        return extract_headers()


async def test_one_vendor_hook_resolves_the_same_principal_on_both_adapters() -> None:
    """Register A8. Assert the EXPECTED principal, not merely that the two
    agree — two adapters broken identically (both ``None``, the fail-open
    result) pass an agreement-only check, which is rule 1 at the top of this
    file."""
    import logging

    from baton.identity import Principal
    from baton.integrations._config import SessionResolutionContext
    from baton.integrations.identity_adapter import resolve_principal_via_hook

    expected = Principal(principal_id="employee-4417")
    resolved: dict[str, Any] = {}

    for adapter, extract in (("official", _official_headers), ("standalone", _standalone_headers)):
        headers = extract()
        assert headers is not None, f"{adapter} extracted no headers — the check would be vacuous"
        resolved[adapter] = await resolve_principal_via_hook(
            _vendor_hook,
            SessionResolutionContext(headers=headers, meta=None, tool_name="lookup", arguments={}),
            logger=logging.getLogger("parity"),
        )

    assert resolved["official"] == expected, resolved
    assert resolved["standalone"] == expected, (
        "the hook fell open on standalone: it raised KeyError, was caught, and "
        "the vendor gets a null principal_id on every event"
    )


async def test_a_duplicated_header_line_still_resolves_differently_per_adapter() -> None:
    """The divergence this change does NOT close, pinned so the docstring
    claiming it cannot go stale silently.

    A proxy chain appending a second ``x-forwarded-user`` is ordinary. Starlette
    answers with the FIRST line; fastmcp's ``get_http_headers`` builds a dict by
    assigning every line in turn, so it answers with the LAST. That collapse
    happens upstream of ``CaseInsensitiveHeaders`` — the first value is already
    gone by the time the SDK sees a dict — so folding case cannot repair it.

    Asserting the two DISAGREE is deliberate. If a future upstream makes them
    agree this test reds, which is the notification we want: the residual
    divergence recorded in ``CaseInsensitiveHeaders`` would then be false, and
    a docstring nobody re-measures is how A8 shipped in the first place.
    """
    from fastmcp.server.dependencies import get_http_headers
    from fastmcp.server.http import set_http_request

    from baton.integrations._config import CaseInsensitiveHeaders

    request = fake_http_request([(b"x-forwarded-user", b"alice"), (b"x-forwarded-user", b"bob")])

    assert request.headers.getlist("x-forwarded-user") == ["alice", "bob"]
    assert request.headers["X-Forwarded-User"] == "alice", "official: Starlette returns the first"

    with set_http_request(request):
        standalone = CaseInsensitiveHeaders(get_http_headers(include_all=True))
    assert standalone["X-Forwarded-User"] == "bob", "standalone: the dict build keeps the last"
