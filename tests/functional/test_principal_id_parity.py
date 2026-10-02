"""Both MCP adapters must produce the SAME ``principal`` from the same token,
read through the same ready-made hook.

⚠ Since the SDK's own token rung was deleted, nothing reads the token unless a
hook does, so every token test here passes ``principal_from_oauth_sub`` — and
one test pins that a token with NO hook yields nothing on either adapter.

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

from baton import principal_from_oauth_email, principal_from_oauth_sub
from baton.events import PrincipalWire
from tests._asgi import fake_http_request, starlette_headers
from tests._event_helpers import principal_of, without_surface_snapshots

pytestmark = pytest.mark.functional

TENANT = "tenant-parity"
HMAC_KEY = b"parity-principal-id-key"
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
    await _run_official_path(
        official_path,
        _official_token(),
        "hashed",
        monkeypatch,
        resolve_principal=principal_from_oauth_sub,
    )
    await _run_standalone_path(
        standalone_path,
        _standalone_token(),
        "hashed",
        monkeypatch,
        resolve_principal=principal_from_oauth_sub,
    )

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
    await _run_official_path(
        official_path,
        _official_token(),
        "hashed",
        monkeypatch,
        resolve_principal=principal_from_oauth_sub,
    )
    await _run_standalone_path(
        standalone_path,
        _standalone_token(),
        "hashed",
        monkeypatch,
        resolve_principal=principal_from_oauth_sub,
    )

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
    await _run_official_path(
        official_path,
        _official_token(),
        "raw",
        monkeypatch,
        resolve_principal=principal_from_oauth_sub,
    )
    await _run_standalone_path(
        standalone_path,
        _standalone_token(),
        "raw",
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
    await _run_official_path(official_path, None, "hashed", monkeypatch)
    await _run_standalone_path(standalone_path, None, "hashed", monkeypatch)

    assert _principals(official_path) == {None}
    assert _principals(standalone_path) == {None}


async def test_a_verified_token_with_NO_hook_yields_no_principal_on_either_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deletion, end to end. Until this release both adapters read the
    token's ``sub`` themselves whenever no hook answered; identity is now the
    vendor's choice, so a usable token on its own puts nothing on the wire."""
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), "hashed", monkeypatch)
    await _run_standalone_path(standalone_path, _standalone_token(), "hashed", monkeypatch)

    assert _principals(official_path) == {None}
    assert _principals(standalone_path) == {None}


async def test_the_email_hook_reads_the_token_on_both_adapters_and_both_emit_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SessionResolutionContext.access_token`` is filled by four call sites
    (two adapters x tool call and annotation). ``_one_principal`` collapses
    every event of a run to one value, so a site that forgot the token yields
    a second, ``None``, entry and fails here."""
    from baton.identity import hash_principal_id

    claims = {"sub": "opaque-123", "email": "alice@acme.example", "iss": CLAIMS["iss"]}
    expected = hash_principal_id(
        claims["email"], tenant_id=TENANT, key=HMAC_KEY, issuer=claims["iss"]
    )
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(
        official_path,
        _official_token(claims),
        "hashed",
        monkeypatch,
        resolve_principal=principal_from_oauth_email,
    )
    await _run_standalone_path(
        standalone_path,
        _standalone_token(claims),
        "hashed",
        monkeypatch,
        resolve_principal=principal_from_oauth_email,
    )
    for path in (official_path, standalone_path):
        assert _one_principal(path) == {
            "id": expected,
            "source": "asserted",
            "form": "hashed",
        }, path.name
        blob = path.read_text()
        # The tool's own argument is ``"alice"``, so the check is on the address.
        assert "@acme.example" not in blob, f"the address leaked in {path.name}"


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
    from baton.identity import Principal, hash_principal_id

    expected = hash_principal_id(HOOK_SUB, tenant_id=TENANT, key=HMAC_KEY, issuer=HOOK_ISS)
    assert ":" not in expected, (
        "no tag rides a hashed value from 0.8.11 — an asserted principal hashes "
        "to the same bare digest as any other, and its provenance rides "
        "`principal.source`"
    )

    hook = _hook(Principal(principal_id=HOOK_SUB, issuer=HOOK_ISS))
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, "hashed", monkeypatch, resolve_principal=hook)
    await _run_standalone_path(standalone_path, None, "hashed", monkeypatch, resolve_principal=hook)

    for path in (official_path, standalone_path):
        assert _one_principal(path) == {
            "id": expected,
            "source": "asserted",
            "form": "hashed",
        }, path.name


async def test_a_hook_that_ignores_the_token_is_never_overridden_by_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token here is the SAME one every other test in this file uses, so its
    value is known: a producer still reading it behind the hook would emit the
    hash of ``CLAIMS["sub"]`` somewhere on these runs.
    """
    from baton.identity import Principal, hash_principal_id

    asserted = hash_principal_id(HOOK_SUB, tenant_id=TENANT, key=HMAC_KEY, issuer=HOOK_ISS)
    from_token = hash_principal_id(
        CLAIMS["sub"], tenant_id=TENANT, key=HMAC_KEY, issuer=CLAIMS["iss"]
    )
    assert asserted != from_token

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
        got = _one_principal(path)
        assert got == {"id": asserted, "source": "asserted", "form": "hashed"}, path.name
        assert got["id"] != from_token, f"{path.name} used the token despite a hook"


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
    await _run_official_path(
        official_path, _official_token(), "hashed", monkeypatch, resolve_principal=boom
    )
    await _run_standalone_path(
        standalone_path, _standalone_token(), "hashed", monkeypatch, resolve_principal=boom
    )

    assert _principals(official_path) == {None}
    assert _principals(standalone_path) == {None}


async def test_an_async_hook_works_on_both_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync or async, matching ``scrubber``. A
    vendor resolving identity will usually be doing I/O to do it."""
    from baton.identity import Principal, hash_principal_id

    expected = hash_principal_id(HOOK_SUB, tenant_id=TENANT, key=HMAC_KEY, issuer=None)

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
    from baton.identity import Principal, hash_principal_id

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
        return hash_principal_id(sub, tenant_id=TENANT, key=HMAC_KEY, issuer=None)

    assert h("user-of-lookup") in got
    assert h(f"user-of-{annotate}") in got, (
        "the annotation path did not consult the hook — a session would carry "
        "two provenances for one person"
    )
    assert len(got) == 2, got


async def test_raw_mode_KEEPS_the_provenance_it_used_to_forfeit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The defect this whole change exists to fix, asserted directly.**

    This test previously pinned the OPPOSITE and was right to: provenance was
    encoded in the scheme tag, ``"raw"`` mode emits no tag, so an asserted
    principal and an attested one reached the wire as indistinguishable bare
    strings and no consumer could recover which it held. SPEC §11.4 conceded it
    in its own derivation row.

    ``source`` is a member now, so it survives a mode that has no tag to carry
    it. The value stays verbatim — that is what ``"raw"`` means and it has not
    changed — and the classification travels beside it.
    """
    from baton.identity import Principal

    hook = _hook(Principal(principal_id=HOOK_SUB, issuer=HOOK_ISS))
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, "raw", monkeypatch, resolve_principal=hook)
    await _run_standalone_path(standalone_path, None, "raw", monkeypatch, resolve_principal=hook)

    for path in (official_path, standalone_path):
        assert _one_principal(path) == {
            "id": HOOK_SUB,
            "source": "asserted",
            "form": "raw",
        }, path.name


async def test_the_sub_hook_emits_the_SAME_digest_the_deleted_token_rung_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The migration claim in SPEC §13, asserted.** A vendor who relied on
    the deleted rung restores it by passing ``principal_from_oauth_sub``, and
    their users keep their pseudonyms: one ``(tenant, sub, iss)`` hashes to the
    same digest it always did, and only ``source`` changes. The expected value
    is computed exactly as the deleted rung computed it.

    Also the hook-vs-hand-written agreement: a vendor hook returning the same
    ``Principal`` by hand must land on the same digest, or the ready-made hook
    is doing something a vendor's own cannot reproduce.
    """
    from baton.identity import Principal, hash_principal_id

    rung_digest = hash_principal_id(
        CLAIMS["sub"], tenant_id=TENANT, key=HMAC_KEY, issuer=CLAIMS["iss"]
    )
    by_hand = tmp_path / "hand.jsonl"
    via_hook = tmp_path / "hook.jsonl"
    same = Principal(principal_id=CLAIMS["sub"], issuer=CLAIMS["iss"])
    await _run_official_path(by_hand, None, "hashed", monkeypatch, resolve_principal=_hook(same))
    await _run_official_path(
        via_hook,
        _official_token(),
        "hashed",
        monkeypatch,
        resolve_principal=principal_from_oauth_sub,
    )

    for path in (by_hand, via_hook):
        assert _one_principal(path) == {
            "id": rung_digest,
            "source": "asserted",
            "form": "hashed",
        }, path.name


async def test_source_stays_asserted_in_every_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §11.4: ``form`` says nothing about trust and ``source`` says nothing
    about privacy. With one live source, what remains to pin is that neither
    the mode nor the KIND of hook (a fixed value, or the token read) moves
    ``source`` — a producer that still stamped a token read ``"attested"``
    fails the token rows.
    """
    from baton.identity import Principal

    hook = _hook(Principal(principal_id=HOOK_SUB, issuer=HOOK_ISS))
    cells = 0
    for mode, form in (("hashed", "hashed"), ("raw", "raw")):
        for label, token, resolver in (
            ("token", _official_token(), principal_from_oauth_sub),
            ("fixed", None, hook),
        ):
            path = tmp_path / f"{label}-{mode}.jsonl"
            await _run_official_path(path, token, mode, monkeypatch, resolve_principal=resolver)
            got = _one_principal(path)
            assert got["source"] == "asserted", got
            assert got["form"] == form, got
            cells += 1

    # Guards against a vacuous pass — a loop that ran zero times.
    assert cells == 4, f"the matrix ran {cells} cells"


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
