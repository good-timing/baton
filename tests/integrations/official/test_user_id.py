"""``user_id`` on the OFFICIAL mcp SDK adapter, across the mcp matrix.

Here rather than in ``tests/functional/`` for the same reason
``test_agent_runtime.py`` is: ``mcp-matrix`` runs
``tests/integrations/official/`` against mcp 1.20.0 / 1.25.0 / 1.27.2 / 2.0.0
and nothing else, and this field is version-sensitive in a way nothing else in
the suite is — ``AccessToken`` gained ``claims`` and ``subject`` somewhere in
(1.25, 1.27], so **two of those four legs cannot carry identity at all.**

That is the point of putting it here. On 1.20 and 1.25 these tests assert the
documented degrade (no ``claims`` ⇒ no ``user_id``, no crash) against the REAL
``AccessToken`` class rather than a stub of it, which is the only way to know
the degrade still holds when the field genuinely does not exist. On 1.27+ they
assert the value is produced. ``_CLAIMS_SUPPORTED`` selects which, and it is
read off the real class so it cannot drift from what is installed.

The token is injected by patching ``_auth.get_access_token_or_none`` — the
module attribute, so one patch covers both the tool-wrap and annotation paths.
That boundary is deliberate: standing up a real OAuth server here would be
testing mcp's bearer middleware, which is not ours. What IS ours is everything
downstream of "a verified token exists", and V4 (2026-09-07) already measured
the upstream half end-to-end with real HTTP servers and real verifiers.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.auth.provider import AccessToken

from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.sinks import FileSink
from tests._event_helpers import without_surface_snapshots
from tests._mcp_session import connected_session

#: Whether the installed ``mcp`` can carry claims at all. False on 1.20 / 1.25,
#: where pydantic's default ``extra="ignore"`` SILENTLY DROPS a ``claims=``
#: kwarg — so a token built below simply has no claims to read.
_CLAIMS_SUPPORTED = "claims" in AccessToken.model_fields

HMAC_KEY = b"official-user-id-key"


def _read(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _token(**kw: Any) -> AccessToken:
    """A real ``AccessToken`` for the installed version.

    On the old band the ``claims`` kwarg is accepted and discarded, which is
    exactly the production behaviour being asserted — a vendor verifier that
    passes claims there loses them too.
    """
    return AccessToken(token="jwt", client_id="acme-desktop-app", scopes=[], **kw)


async def _drive(
    events_path: Path,
    token: Any,
    *,
    mode: str = "hashed",
    hmac_key: bytes | None = HMAC_KEY,
    resolve_user: Any = None,
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """One tool call + one annotation call, with ``token`` as the caller."""
    from baton.integrations.official import _auth

    monkeypatch.setattr(_auth, "get_access_token_or_none", lambda: token)

    mcp = FastMCP("user-id-official")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="uid",
            vendor_display_name="Identity Vendor",
            consent_token="ct_uid",
            sink=FileSink(str(events_path)),
            tenant_id="tenant-official",
            user_id_mode=mode,
            user_id_hmac_key=hmac_key,
            resolve_user=resolve_user,
        ),
    )
    try:
        async with connected_session(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
            await client.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "look something up", "signal_type": "failure"},
            )
    finally:
        await handle.aclose()
    events = without_surface_snapshots(_read(events_path))
    assert events, "no events captured — every assertion below would be vacuous"
    return events


async def test_every_event_of_a_call_carries_the_same_user_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Including the annotation event, which is its own regression class.

    ``agent_runtime`` shipped broken on exactly this path — the annotation tool
    took no ``Context``, so it reported the install-time default while the
    calls around it were detected. Asserted per ``event_type`` so a build where
    only one path works names which one.
    """
    events = await _drive(
        tmp_path / "e.jsonl",
        _token(claims={"sub": "alice", "iss": "https://idp"}),
        monkeypatch=monkeypatch,
    )
    by_type = {ev["event_type"]: ev.get("user_id") for ev in events}
    assert "annotation" in by_type, f"no annotation event, got {sorted(by_type)}"
    assert "tool_call_start" in by_type, f"no tool_call_start, got {sorted(by_type)}"

    if _CLAIMS_SUPPORTED:
        assert all(v is not None and v.startswith("h1:") for v in by_type.values()), by_type
        assert len(set(by_type.values())) == 1, f"one caller, two user_ids: {by_type}"
    else:
        # mcp < 1.27: the field cannot be carried, so it is absent everywhere.
        # Absent, not wrong — and above all not an exception.
        assert set(by_type.values()) == {None}, by_type


async def test_a_vendor_subclass_carries_identity_on_every_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the ``[mcp]`` floor was NOT raised to 1.27.

    A vendor's ``TokenVerifier`` returns whatever class it likes, so one that
    declares ``claims`` on a subclass is read correctly even on 1.20 — where
    the base class has no such field. This test is the one that must pass on
    EVERY matrix leg; if it ever fails on the old band, the escape hatch that
    licensed keeping the floor is gone and the floor decision needs re-opening.
    """

    class VendorToken(AccessToken):  # type: ignore[misc]
        claims: dict[str, Any] | None = None

    events = await _drive(
        tmp_path / "e.jsonl",
        VendorToken(token="jwt", client_id="acme-app", scopes=[], claims={"sub": "carol"}),
        monkeypatch=monkeypatch,
    )
    user_ids = {ev.get("user_id") for ev in events}
    assert user_ids != {None}, "a vendor-declared claims field was not read"
    assert all(v is not None and v.startswith("h1:") for v in user_ids), user_ids


async def test_an_unauthenticated_call_emits_without_the_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every stdio call, and most HTTP ones. Events must still flow."""
    events = await _drive(tmp_path / "e.jsonl", None, monkeypatch=monkeypatch)
    assert {ev.get("user_id") for ev in events} == {None}
    assert any(ev["event_type"] == "tool_call_end" for ev in events), (
        "the call itself must still complete and emit"
    )


async def test_the_raw_principal_never_reaches_the_wire_in_hashed_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted against the RAW FILE, not the parsed field.

    The residency contract is about bytes leaving the process, so checking
    ``ev["user_id"]`` alone would miss the identity riding some other key —
    which is exactly the leak baton-extmcp 0.2.0 had to remove, where raw
    identity travelled in ``runtime_meta`` while ``user_id`` looked correct.
    """
    events_path = tmp_path / "e.jsonl"
    await _drive(
        events_path,
        _token(claims={"sub": "alice@acme.example", "iss": "https://idp.example"}),
        monkeypatch=monkeypatch,
    )
    blob = events_path.read_text()
    assert "alice@acme.example" not in blob
    assert "idp.example" not in blob


async def test_raw_mode_puts_the_principal_on_the_wire_deliberately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in half. If this ever passes by accident on the default config,
    the default has changed and the residency posture went with it."""
    if not _CLAIMS_SUPPORTED:
        pytest.skip("mcp < 1.27 cannot carry claims; nothing to read in either mode")

    events = await _drive(
        tmp_path / "e.jsonl",
        _token(claims={"sub": "alice@acme.example"}),
        mode="raw",
        hmac_key=None,
        monkeypatch=monkeypatch,
    )
    user_ids = {ev.get("user_id") for ev in events}
    assert user_ids == {"alice@acme.example"}, user_ids


async def test_hashed_mode_without_a_key_still_emits_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open-skip: ``user_id`` is additive analytics, never a gate."""
    events = await _drive(
        tmp_path / "e.jsonl",
        _token(claims={"sub": "alice"}),
        hmac_key=None,
        monkeypatch=monkeypatch,
    )
    assert {ev.get("user_id") for ev in events} == {None}
    assert any(ev["event_type"] == "tool_call_end" for ev in events)


def test_an_invalid_mode_is_refused_at_install() -> None:
    """D3's refusal posture: a misconfiguration that would silently emit the
    wrong thing fails loudly at install instead."""
    mcp = FastMCP("user-id-invalid")
    with pytest.raises(ValueError, match="user_id_mode"):
        install_baton(
            mcp,
            VendorConfig(
                vendor_id="uid",
                vendor_display_name="Identity Vendor",
                consent_token="ct_uid",
                user_id_mode="plaintext",
            ),
        )


# ---------------------------------------------------------------------------
# The ``resolve_user`` hook (N11) on this matrix.
#
# The rest of the hook's coverage lives in ``tests/functional/`` and
# ``tests/test_identity_hook.py``. It is duplicated HERE, thinly, for the
# reason at the top of this file: ``mcp-matrix`` runs
# ``tests/integrations/official/`` and nothing else, so nothing else in the
# suite exercises the official adapter against mcp 1.20 / 1.25 / 1.27 / 2.0.
#
# The version-sensitive part is not the hashing — it is that a hook makes
# ``_extract_headers_from_context`` run on the ANNOTATION path for the first
# time, and header access differs across the mcp majors (1.x has no
# ``Context.headers``; the extractor reaches through ``request_context.request``
# and must swallow the ``ValueError`` that raises outside a live request).
# ``connected_session`` is in-process with no HTTP request, which is exactly
# the shape that raises, so these tests fail loudly if the guard stops holding
# on any leg.
#
# Unlike everything above, these do NOT depend on ``claims`` and therefore run
# identically on all four legs — a hook is the one identity path that works
# where the token path cannot.


def _fixed_hook(user_id: str) -> Any:
    from baton.identity import Principal

    def resolve(_ctx: Any) -> Any:
        return Principal(user_id=user_id)

    return resolve


def _per_call_hook() -> Any:
    """A principal derived from the context the hook was handed, so the two
    emit paths produce two different values on one run."""
    from baton.identity import Principal

    def resolve(ctx: Any) -> Any:
        return Principal(user_id=f"user-of-{ctx.tool_name}")

    return resolve


async def test_a_hook_carries_identity_on_every_leg_including_the_claimless_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``token=None`` is stdio, and it is also 1.20/1.25 with nothing readable.

    The expected value is computed here rather than pattern-matched, so a hash
    that is merely *present* cannot pass for the right one.
    """
    from baton.identity import VENDOR_HASH_SCHEME, hash_user_id

    expected = hash_user_id(
        "employee-4417", tenant_id="tenant-official", key=HMAC_KEY, scheme=VENDOR_HASH_SCHEME
    )
    events = await _drive(
        tmp_path / "e.jsonl",
        None,
        resolve_user=_fixed_hook("employee-4417"),
        monkeypatch=monkeypatch,
    )
    got = {ev.get("user_id") for ev in events}
    assert got == {expected}, got
    assert expected.startswith("v1:")


async def test_the_annotation_path_consults_the_hook_on_every_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header extractor runs here for the first time on the annotation
    path. If it raised instead of degrading on some mcp version, the annotation
    event would carry no ``user_id`` while the tool call carried one — one
    session, two actors, and green everywhere else."""
    events = await _drive(
        tmp_path / "e.jsonl",
        None,
        resolve_user=_per_call_hook(),
        monkeypatch=monkeypatch,
    )
    by_type = {ev["event_type"]: ev.get("user_id") for ev in events}
    assert "annotation" in by_type, f"no annotation event captured: {list(by_type)}"
    assert by_type["annotation"] is not None, "the annotation path skipped the hook"
    assert all(v is not None for v in by_type.values()), by_type
    # Two distinct tool names ⇒ two distinct principals ⇒ the hook really did
    # run per call rather than once at install.
    assert len({v for v in by_type.values()}) > 1, by_type


async def test_a_hook_that_raises_leaves_the_call_and_the_events_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open against the REAL server on every leg, not a stub of one."""

    def boom(_ctx: Any) -> Any:
        raise RuntimeError("the vendor's directory service is down")

    events = await _drive(tmp_path / "e.jsonl", None, resolve_user=boom, monkeypatch=monkeypatch)
    assert events, "the hook's exception cost the capture"
    assert {ev.get("user_id") for ev in events} == {None}


async def test_headers_are_extracted_once_per_call_when_a_hook_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity hook and the session ladder both want the request headers.

    The comment gating the hook context says header extraction is not free and
    must not be paid by servers that never configure the field — so paying for
    it TWICE on the servers that do would make that comment a lie. The value is
    hoisted once and threaded into both.
    """
    from baton.integrations.official import _tool_wrap

    calls = 0
    real = _tool_wrap._extract_headers_from_context

    def counting(ctx: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(ctx)

    monkeypatch.setattr(_tool_wrap, "_extract_headers_from_context", counting)
    await _drive(
        tmp_path / "e.jsonl", None, resolve_user=_fixed_hook("employee-1"), monkeypatch=monkeypatch
    )
    # EXACTLY one, for the tool call. The annotation path imported the helper
    # by name and holds its own bound reference, so this patch point does not
    # observe it — that is the tool-call path alone, which is the one where the
    # identity hook and the session ladder both want the value.
    #
    # An exact count, not a ceiling: the first attempt at this fix gave the
    # ladder a ``None`` default it re-extracted from, so on stdio — where
    # ``None`` is also the correct ANSWER — nothing changed while the diff
    # looked right. A ``<=`` assertion passed that version too.
    assert calls == 1, f"headers extracted {calls} times for one tool call, expected 1"
