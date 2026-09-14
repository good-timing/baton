"""Both MCP adapters must produce the SAME ``principal_id`` from the same token.

The companion to ``test_agent_runtime_parity.py``, and it exists for the same
recorded reason: ``detect_agent_runtime`` lived under one adapter's package,
the other never called it, and every event that adapter emitted carried
``"unknown"`` through a rename, a release and a CI matrix — because each
adapter's own suite asserted only about itself. ``identity_adapter.py`` is
shared from the first commit specifically so that cannot recur, and this file
is what pins it.

It follows the same two rules as the runtime parity test:

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

from tests._asgi import fake_http_request, starlette_headers
from tests._event_helpers import without_surface_snapshots

pytestmark = pytest.mark.functional

TENANT = "tenant-parity"
HMAC_KEY = b"parity-principal-id-key"
CLAIMS = {"sub": "alice@acme.example", "iss": "https://idp.example"}


def _read_events(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _official_token() -> Any:
    from mcp.server.auth.provider import AccessToken

    if "claims" not in AccessToken.model_fields:
        pytest.skip("mcp < 1.27 cannot carry claims; parity is asserted on 1.27+ only")
    return AccessToken(token="jwt", client_id="acme-app", scopes=[], claims=CLAIMS)


def _standalone_token() -> Any:
    from fastmcp.server.dependencies import AccessToken

    return AccessToken(token="jwt", client_id="acme-app", scopes=[], claims=CLAIMS)


async def _run_official_path(
    events_path: Path,
    token: Any,
    mode: str,
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
            principal_id_mode=mode,
            principal_id_hmac_key=HMAC_KEY,
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
    mode: str,
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
            principal_id_mode=mode,
            principal_id_hmac_key=HMAC_KEY,
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
    events = without_surface_snapshots(_read_events(path))
    assert events, f"no events captured at {path} — the assertion would be vacuous"
    return {ev.get("principal_id") for ev in events}


async def test_both_adapters_hash_one_principal_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expected value first, then agreement.

    The expected value is computed independently here rather than by comparing
    the two runs, because two adapters that both dropped the field agree
    perfectly.
    """
    from baton.identity import hash_principal_id

    expected = hash_principal_id(
        CLAIMS["sub"], tenant_id=TENANT, key=HMAC_KEY, issuer=CLAIMS["iss"]
    )

    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), "hashed", monkeypatch)
    await _run_standalone_path(standalone_path, _standalone_token(), "hashed", monkeypatch)

    official = _principal_ids(official_path)
    standalone = _principal_ids(standalone_path)

    assert official == {expected}, f"official adapter: {official}"
    assert standalone == {expected}, f"standalone adapter: {standalone}"
    assert official == standalone


async def test_neither_adapter_leaks_the_principal_in_hashed_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked against the raw files on both sides.

    One adapter leaking while the other does not is the shape this whole file
    exists to catch, and a residency leak is the worst version of it.
    """
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), "hashed", monkeypatch)
    await _run_standalone_path(standalone_path, _standalone_token(), "hashed", monkeypatch)

    for path in (official_path, standalone_path):
        blob = path.read_text()
        assert CLAIMS["sub"] not in blob, f"raw principal found in {path.name}"
        assert CLAIMS["iss"] not in blob, f"issuer found in {path.name}"


async def test_both_adapters_agree_in_raw_mode_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw mode is a wire contract like any other — two sensors watching one
    client must not disagree about who it is, whichever mode is configured."""
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), "raw", monkeypatch)
    await _run_standalone_path(standalone_path, _standalone_token(), "raw", monkeypatch)

    assert _principal_ids(official_path) == {CLAIMS["sub"]}
    assert _principal_ids(standalone_path) == {CLAIMS["sub"]}


async def test_both_adapters_drop_the_field_when_unauthenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, "hashed", monkeypatch)
    await _run_standalone_path(standalone_path, None, "hashed", monkeypatch)

    assert _principal_ids(official_path) == {None}
    assert _principal_ids(standalone_path) == {None}


# ---------------------------------------------------------------------------
# The vendor identity hook (N11) — the ASSERTED provenance.
#
# These run through the same two drivers as everything above, which is the
# point: each driver calls a tool AND the annotation tool, and ``_principal_ids``
# collapses every emitted event to a SET. A hook wired into the tool-call path
# but not the annotation path yields two values on one run and fails here,
# without a test that names the annotation path at all.

HOOK_SUB = "employee-4417"
HOOK_ISS = "https://sso.acme.internal"


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
    ASGI. Before the hook this run emitted no ``principal_id`` at all, on either
    adapter. The expected value is computed here rather than compared between
    runs, for the reason at the top of this file.
    """
    from baton.identity import VENDOR_HASH_SCHEME, Principal, hash_principal_id

    expected = hash_principal_id(
        HOOK_SUB, tenant_id=TENANT, key=HMAC_KEY, issuer=HOOK_ISS, scheme=VENDOR_HASH_SCHEME
    )
    assert expected.startswith("v1:")

    hook = _hook(Principal(principal_id=HOOK_SUB, issuer=HOOK_ISS))
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, "hashed", monkeypatch, resolve_principal=hook)
    await _run_standalone_path(standalone_path, None, "hashed", monkeypatch, resolve_principal=hook)

    official = _principal_ids(official_path)
    standalone = _principal_ids(standalone_path)
    assert official == {expected}, f"official adapter: {official}"
    assert standalone == {expected}, f"standalone adapter: {standalone}"


async def test_the_hook_wins_over_a_verified_token_and_says_so_in_the_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence, and the tag that keeps it honest.

    The token here is the SAME one every other test in this file uses, so the
    attested value is known: if precedence were the other way round, these runs
    would emit the ``h1:`` hash of ``CLAIMS["sub"]``. Asserting the tag as well
    as the value is what distinguishes "the hook won" from "the hook happened
    to produce the same string".
    """
    from baton.identity import VENDOR_HASH_SCHEME, Principal, hash_principal_id

    asserted = hash_principal_id(
        HOOK_SUB, tenant_id=TENANT, key=HMAC_KEY, issuer=HOOK_ISS, scheme=VENDOR_HASH_SCHEME
    )
    attested = hash_principal_id(
        CLAIMS["sub"], tenant_id=TENANT, key=HMAC_KEY, issuer=CLAIMS["iss"]
    )
    assert asserted != attested

    hook = _hook(Principal(principal_id=HOOK_SUB, issuer=HOOK_ISS))
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(
        official_path, _official_token(), "hashed", monkeypatch, resolve_principal=hook
    )
    await _run_standalone_path(
        standalone_path, _standalone_token(), "hashed", monkeypatch, resolve_principal=hook
    )

    for path in (official_path, standalone_path):
        got = _principal_ids(path)
        assert got == {asserted}, f"{path.name}: {got}"
        assert attested not in got, f"{path.name} used the token despite a hook"


async def test_a_hook_that_raises_falls_back_to_the_token_and_events_still_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vendor's bug in their own resolver may not cost them their capture.

    Asserting the ATTESTED value, not merely "not None": a fallback that
    produced nothing would also survive a looser check, and "the hook broke so
    identity vanished" is the failure this guard exists to prevent.
    """
    from baton.identity import hash_principal_id

    attested = hash_principal_id(
        CLAIMS["sub"], tenant_id=TENANT, key=HMAC_KEY, issuer=CLAIMS["iss"]
    )

    def boom(_ctx: Any) -> Any:
        raise RuntimeError("the vendor's directory service is down")

    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(
        official_path, _official_token(), "hashed", monkeypatch, resolve_principal=boom
    )
    await _run_standalone_path(
        standalone_path, _standalone_token(), "hashed", monkeypatch, resolve_principal=boom
    )

    assert _principal_ids(official_path) == {attested}
    assert _principal_ids(standalone_path) == {attested}


async def test_an_async_hook_works_on_both_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync or async, matching ``scrubber``. A
    vendor resolving identity will usually be doing I/O to do it."""
    from baton.identity import VENDOR_HASH_SCHEME, Principal, hash_principal_id

    expected = hash_principal_id(
        HOOK_SUB, tenant_id=TENANT, key=HMAC_KEY, issuer=None, scheme=VENDOR_HASH_SCHEME
    )

    async def resolve(_ctx: Any) -> Any:
        return Principal(principal_id=HOOK_SUB)

    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, "hashed", monkeypatch, resolve_principal=resolve)
    await _run_standalone_path(
        standalone_path, None, "hashed", monkeypatch, resolve_principal=resolve
    )

    assert _principal_ids(official_path) == {expected}
    assert _principal_ids(standalone_path) == {expected}


async def test_the_hook_sees_the_calls_own_context_not_an_install_time_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The difference from the removed ``default_agent_runtime``.

    That was one value fixed at install. This is a callable invoked per
    request, so it can answer differently per call — asserted by returning a
    principal derived from the context the hook was handed, and then finding
    BOTH resulting hashes on the wire.
    """
    from baton.identity import VENDOR_HASH_SCHEME, Principal, hash_principal_id

    def per_call(ctx: Any) -> Any:
        # ``tool_name`` differs between the lookup call and the annotation
        # call, so one hook yields two principals on one run.
        return Principal(principal_id=f"user-of-{ctx.tool_name}")

    official_path = tmp_path / "official.jsonl"
    annotate = await _run_official_path(
        official_path, None, "hashed", monkeypatch, resolve_principal=per_call
    )
    got = _principal_ids(official_path)

    def h(sub: str) -> str:
        return hash_principal_id(
            sub, tenant_id=TENANT, key=HMAC_KEY, issuer=None, scheme=VENDOR_HASH_SCHEME
        )

    assert h("user-of-lookup") in got
    assert h(f"user-of-{annotate}") in got, (
        "the annotation path did not consult the hook — a session would carry "
        "two provenances for one person"
    )
    assert len(got) == 2, got


async def test_raw_mode_does_not_tag_a_hook_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented, not incidental: raw mode forfeits provenance the same way
    it forfeits pseudonymity, so a consumer cannot tell asserted from attested
    there. SPEC §11.4 says so; this pins it rather than letting a future reader
    assume a ``v1:`` prefix survives into raw mode."""
    from baton.identity import Principal

    hook = _hook(Principal(principal_id=HOOK_SUB, issuer=HOOK_ISS))
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, "raw", monkeypatch, resolve_principal=hook)
    await _run_standalone_path(standalone_path, None, "raw", monkeypatch, resolve_principal=hook)

    assert _principal_ids(official_path) == {HOOK_SUB}
    assert _principal_ids(standalone_path) == {HOOK_SUB}


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
