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
        ),
    )
    try:
        async with connected_session(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
            await client.call_tool(
                "uid_annotate",
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
