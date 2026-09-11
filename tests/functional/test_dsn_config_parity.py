"""One DSN, four doors, one resolution — and it must reach the WIRE.

The SDK now has four places a vendor configures capture: the two MCP adapters'
``install_baton`` and the library API's ``Client`` / ``AsyncClient``. Each one
accepts the same packed ``dsn``, and this file drives all four with one string
and asserts they agree.

**It exists because the absence of exactly this test shipped.**
``detect_agent_runtime`` lived under one adapter, the other never called it,
and every event that adapter emitted carried ``agent_runtime: "unknown"``
through a rename, a release and a CI matrix — because each per-adapter suite
asserted its own behaviour and nothing drove both with one input. Config
resolution is the same shape of risk with a worse failure: a door that resolved
the workspace differently would file a customer's events under the wrong
account, and every per-door test would stay green.

**Asserted on the POSTed envelope, not on the config object.** A resolver that
computes the right values and a sink that never carries them are the same
outcome for the customer. The collector here is a real HTTP server, so the
sink's URL and its bearer are proven too — which is the half a config-level
assertion cannot reach.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from baton.events import DEFAULT_CONSENT_TOKEN

pytestmark = pytest.mark.functional

WORKSPACE = "ten_655b084e118b43f88992ee6357fcc23c"
KEY = "baton_pk_" + "z" * 43
SERVER = "echo-server"


@pytest.fixture
def collector(httpserver: HTTPServer) -> tuple[HTTPServer, list[dict[str, Any]], list[str]]:
    """A real collector: captures envelope bodies AND the bearer they arrived with."""
    events: list[dict[str, Any]] = []
    bearers: list[str] = []

    def _handler(req: Request) -> Response:
        events.append(json.loads(req.data.decode("utf-8")))
        bearers.append(req.headers.get("Authorization", ""))
        return Response("", status=204)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(_handler)
    return httpserver, events, bearers


def _dsn_for(server: HTTPServer) -> str:
    # ``url_for("")`` yields the origin with a trailing slash; the grammar
    # ignores one, and building the DSN by hand here keeps the test honest
    # about what a minted string looks like.
    origin = server.url_for("").rstrip("/")
    scheme, _, authority = origin.partition("://")
    return f"{scheme}://{KEY}@{authority}/{WORKSPACE}/{SERVER}"


def _identity(events: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    """(tenant_id, vendor_id, consent_token) over every captured envelope.

    Empty input fails loudly rather than returning an empty set: a driver that
    silently emitted nothing would make every assertion below vacuously true,
    which is how a correlation rig once reported zero mispairs while testing
    nothing at all.
    """
    assert events, "no events reached the collector — the driver is broken, not the SDK"
    return {(ev["tenant_id"], ev["vendor_id"], ev["consent_token"]) for ev in events}


EXPECTED = {(WORKSPACE, SERVER, DEFAULT_CONSENT_TOKEN)}


async def _drive_official(dsn: str) -> None:
    from baton.install import install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from tests._mcp_session import connected_session

    mcp = FastMCP("dsn-official")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(mcp, dsn=dsn)
    try:
        async with connected_session(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
    finally:
        await handle.aclose()


async def _drive_standalone(dsn: str) -> None:
    from fastmcp import Client, FastMCP

    from baton.install import install_baton

    mcp: Any = FastMCP("dsn-standalone")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(mcp, dsn=dsn)
    try:
        async with Client(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
    finally:
        await handle.aclose()


async def _drive_async_client(dsn: str) -> None:
    from baton import AsyncClient

    client = AsyncClient(dsn=dsn)
    try:
        async with client.trace(tool_name="lookup") as trace:
            trace.observed({"found": True})
    finally:
        await client.aclose()


def _drive_sync_client(dsn: str) -> None:
    """The sync twin, driven separately on purpose.

    ``Client`` and ``AsyncClient`` resolve config through the same helper now,
    but this repo has already been fooled once by assuming the sync half of a
    pair was covered because the async half was: a mutation "verified" against
    ``AsyncTrace`` reported red off the async site while the sync site it also
    replaced was inert.
    """
    from baton import Client

    client = Client(dsn=dsn)
    try:
        with client.trace(tool_name="lookup") as trace:
            trace.observed({"found": True})
    finally:
        client.close()


async def test_all_four_doors_resolve_one_dsn_to_one_identity(
    collector: tuple[HTTPServer, list[dict[str, Any]], list[str]],
) -> None:
    server, events, _bearers = collector
    dsn = _dsn_for(server)

    per_door: dict[str, set[tuple[str, str, str]]] = {}
    for name, drive in (
        ("official", _drive_official),
        ("standalone", _drive_standalone),
        ("async-client", _drive_async_client),
    ):
        events.clear()
        await drive(dsn)
        per_door[name] = _identity(events)

    events.clear()
    _drive_sync_client(dsn)
    per_door["sync-client"] = _identity(events)

    # The EXPECTED value, not merely agreement: four doors broken identically
    # — every one of them defaulting tenant_id to vendor_id, say — would pass
    # an agreement-only check while sending every customer's events to the
    # wrong account.
    for name, identity in per_door.items():
        assert identity == EXPECTED, f"{name} resolved {identity}, expected {EXPECTED}"
    assert len(set(map(frozenset, per_door.values()))) == 1


async def test_the_sink_posts_to_the_dsns_origin_with_its_key(
    collector: tuple[HTTPServer, list[dict[str, Any]], list[str]],
) -> None:
    """S2's real claim: the SDK BUILDS the sink, so the key never becomes a
    second thing the customer wires up.

    The path the request arrived on is asserted by the collector's own route —
    ``/v0/events`` and nothing else is registered, so a parser that folded the
    workspace and server segments into the origin would 500 here rather than
    quietly pass.
    """
    server, events, bearers = collector
    await _drive_standalone(_dsn_for(server))

    assert events, "nothing arrived at the collector"
    assert set(bearers) == {f"Bearer {KEY}"}


class TestTheRulesAreTheSameOnBothDoors:
    """Each rule asserted on the install path AND the library path. A rule that
    holds on one door and not the other is the exact failure this file exists
    to catch, and 'it is the same helper' is an implementation detail that a
    future refactor is free to break."""

    def test_a_dsn_beside_an_explicit_value_raises(self) -> None:
        from fastmcp import FastMCP

        from baton import Client
        from baton.install import install_baton
        from baton.integrations._config import VendorConfig
        from baton.sinks import StdoutSink

        dsn = f"https://{KEY}@h.example.com/{WORKSPACE}/{SERVER}"

        for kwargs in (
            {"vendor_id": "other"},
            {"tenant_id": "ten_other"},
            {"sink": StdoutSink()},
        ):
            with pytest.raises(ValueError, match="already supplies it"):
                install_baton(FastMCP("x"), VendorConfig(dsn=dsn, **kwargs))  # type: ignore[arg-type]
            with pytest.raises(ValueError, match="already supplies it"):
                Client(dsn=dsn, **kwargs)  # type: ignore[arg-type]

    def test_a_stale_environment_does_not_outrank_a_dsn(
        self,
        collector: tuple[HTTPServer, list[dict[str, Any]], list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The shape this rule exists for, and it is the common one.

        A server being RE-onboarded has last install's ``.env`` sitting beside
        the new inline DSN — five ``BATON_*`` variables naming the old
        identity. If the DSN's values fell through to the environment the way
        an unset field does, the stale file would win silently and the events
        would arrive under the previous server's name.
        """
        server, events, _ = collector
        monkeypatch.setenv("BATON_VENDOR_ID", "stale-vendor")
        monkeypatch.setenv("BATON_TENANT_ID", "ten_stale")
        monkeypatch.setenv("BATON_CONSENT_TOKEN", "ct-stale")

        events.clear()
        _drive_sync_client(_dsn_for(server))
        identities = _identity(events)

        assert {(t, v) for t, v, _ in identities} == {(WORKSPACE, SERVER)}
        # ⚠ consent_token is the ONE value the environment still supplies. It
        # is not carried by the DSN, so ``BATON_CONSENT_TOKEN`` outranks the
        # default exactly as it always has — an install that sets it keeps
        # working, which is what makes this change invisible to them.
        assert {c for _, _, c in identities} == {"ct-stale"}

    def test_neither_door_invents_a_sink_out_of_nothing(self) -> None:
        from baton import Client

        with pytest.raises(ValueError, match="dsn"):
            Client(vendor_id="v")

    def test_install_baton_with_neither_config_nor_dsn_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastmcp import FastMCP

        from baton.install import install_baton

        monkeypatch.delenv("BATON_DSN", raising=False)
        with pytest.raises(ValueError, match="either a VendorConfig or a dsn"):
            install_baton(FastMCP("x"))

    def test_a_config_and_a_dsn_kwarg_together_raise(self) -> None:
        from fastmcp import FastMCP

        from baton.install import install_baton
        from baton.integrations._config import VendorConfig

        with pytest.raises(ValueError, match="Put the dsn on the config"):
            install_baton(
                FastMCP("x"),
                VendorConfig(vendor_id="v", vendor_display_name="V"),
                dsn=f"https://{KEY}@h.example.com/{WORKSPACE}/{SERVER}",
            )


class TestWhatTheDsnDoesNotTakeOver:
    def test_the_display_name_defaults_to_the_server_slug_verbatim(self) -> None:
        from baton.integrations._config import VendorConfig, resolve_config

        resolved = resolve_config(
            VendorConfig(dsn=f"https://{KEY}@h.example.com/{WORKSPACE}/{SERVER}")
        )
        # Verbatim, not "Echo Server": this string reaches the calling agent,
        # and a capitalisation the vendor never chose is a fabricated name in
        # front of their users.
        assert resolved.vendor_display_name == SERVER

    def test_an_explicit_display_name_survives_a_dsn(self) -> None:
        from baton.integrations._config import VendorConfig, resolve_config

        resolved = resolve_config(
            VendorConfig(
                dsn=f"https://{KEY}@h.example.com/{WORKSPACE}/{SERVER}",
                vendor_display_name="Toybox Pantry",
            )
        )
        assert resolved.vendor_display_name == "Toybox Pantry"

    def test_no_dsn_still_means_stdout(self) -> None:
        """The zero-config dev mode is unchanged: a vendor who wires nothing
        still sees their events on stderr, which is the first thing that proves
        an install works at all."""
        from baton.integrations._config import VendorConfig, resolve_sink
        from baton.sinks import StdoutSink

        assert isinstance(
            resolve_sink(VendorConfig(vendor_id="v", vendor_display_name="V")), StdoutSink
        )


class TestAnAmbientDsnDoesNotBreakAnExplicitInstall:
    """The regression review found, on both doors.

    ``BATON_DSN`` is advertised as the fallback for a hosted vendor. Export it
    for one server, and every OTHER install in that process — a second server,
    a fixture, a CI job — was configured the old explicit way and now died at
    ``install_baton`` naming a ``dsn`` that appears nowhere in the caller's
    code. The environment is a fallback, not something the caller passed.
    """

    def test_the_install_door_still_takes_an_explicit_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastmcp import FastMCP

        from baton.install import install_baton
        from baton.integrations._config import VendorConfig
        from baton.sinks import StdoutSink

        monkeypatch.setenv("BATON_DSN", f"https://{KEY}@h.example.com/{WORKSPACE}/{SERVER}")
        handle = install_baton(
            FastMCP("legacy"),
            VendorConfig(vendor_id="legacy", vendor_display_name="Legacy", sink=StdoutSink()),
        )
        # The explicit config wins outright — not a merge, which would give a
        # server one half of each identity.
        assert handle.vendor_id == "legacy"
        assert isinstance(handle.sink, StdoutSink)

    def test_the_client_door_still_takes_explicit_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from baton import Client
        from baton.sinks import StdoutSink

        monkeypatch.setenv("BATON_DSN", f"https://{KEY}@h.example.com/{WORKSPACE}/{SERVER}")
        client = Client(vendor_id="legacy", sink=StdoutSink())
        try:
            assert (client._vendor_id, client._tenant_id) == ("legacy", "legacy")
        finally:
            client.close()

    def test_an_ambient_dsn_still_configures_a_caller_that_asks_for_nothing(
        self,
        collector: tuple[HTTPServer, list[dict[str, Any]], list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The other half, and the reason the variable exists: with nothing
        explicit to lose to, ``BATON_DSN`` configures the whole install."""
        server, events, _ = collector
        monkeypatch.setenv("BATON_DSN", _dsn_for(server))
        monkeypatch.setenv("BATON_VENDOR_ID", "stale-vendor")

        from baton import Client

        events.clear()
        client = Client()
        try:
            with client.trace(tool_name="lookup") as trace:
                trace.observed({"ok": True})
        finally:
            client.close()
        # It outranks BATON_VENDOR_ID: environment against environment, the
        # packed value is the one someone chose today.
        assert {(t, v) for t, v, _ in _identity(events)} == {(WORKSPACE, SERVER)}


def test_the_bare_install_door_takes_an_ambient_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """``install_baton(mcp)`` with nothing but ``BATON_DSN`` exported.

    A distinct path: ``build_config(None, None)`` has to reach the environment
    before deciding there is nothing to install with, and its "needs either a
    VendorConfig or a dsn" refusal sits on exactly that line. The ambient-DSN
    tests above drive ``Client``; this is the door a hosted vendor uses.
    """
    from fastmcp import FastMCP

    from baton.install import install_baton
    from baton.sinks import HttpSink

    monkeypatch.setenv("BATON_DSN", f"https://{KEY}@h.example.com/{WORKSPACE}/{SERVER}")
    handle = install_baton(FastMCP("ambient"))
    assert handle.vendor_id == SERVER
    assert isinstance(handle.sink, HttpSink)


def test_a_failing_config_never_leaves_a_sink_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HttpSink.__init__`` eagerly builds an ``httpx.AsyncClient``.

    Constructing it before validation meant a config that failed for an
    unrelated reason left that client unreachable and unclosed, printing a
    transport warning on top of the error the vendor actually needs to read.
    Asserted by counting constructions rather than by watching for a warning,
    because a warning that stops being emitted would silently retire the test.
    """
    from baton.integrations import _config

    built: list[str] = []

    class _CountingSink(_config.HttpSink):  # type: ignore[misc,valid-type]
        def __init__(self, url: str, **kwargs: Any) -> None:
            built.append(url)
            super().__init__(url, **kwargs)

    monkeypatch.setattr(_config, "HttpSink", _CountingSink)

    with pytest.raises(ValueError, match="consent_token"):
        _config.resolve_config(
            _config.VendorConfig(
                dsn=f"https://{KEY}@h.example.com/{WORKSPACE}/{SERVER}",
                consent_token="",
            )
        )
    assert built == [], f"a sink was constructed before the config was rejected: {built}"


class TestNothingTheDsnBUILDSPrintsTheBearer:
    """The packed string carries a credential, and the SDK now hands it around:
    a vendor passes one value they never handle again, and it ends up on a
    config that is retained and in a sink that is hung off the handle.

    So "does an accidental print show it?" is asked of every object it reaches,
    not only of the parser. ``baton-ts`` needed this in three places — the
    parsed DSN, the config and the sink — which is why each is checked here
    rather than reasoned about from the one that was reported.
    """

    def _config(self) -> Any:
        from baton.integrations.official.install import VendorConfig

        return VendorConfig(
            vendor_id=SERVER,
            vendor_display_name="Echo",
            consent_token="ct",
            dsn=f"https://{KEY}@ingest.example.com/{WORKSPACE}/{SERVER}",
            user_id_hmac_key="a-vendor-secret-nobody-else-holds",
        )

    def test_the_config_does_not_print_the_dsn(self) -> None:
        """⚠ Measured, and worse than the parser's: ``resolve_config`` copies
        the packed string ONTO the config it returns, so this object outlives
        the parse. A traceback rendering locals, a structured log line taking a
        config, a plain ``print`` — all three wrote a publishable key out."""
        assert KEY not in repr(self._config())

    def test_the_config_does_not_print_the_HMAC_KEY_either(self) -> None:
        """Pre-existing rather than this lane's, and fixed with it: the same
        defect in the same ``repr``, on a field whose own docstring says the
        vendor holds it and Baton never sees it."""
        assert "a-vendor-secret-nobody-else-holds" not in repr(self._config())

    def test_reading_either_field_by_name_is_unchanged(self) -> None:
        config = self._config()
        assert config.dsn is not None and KEY in config.dsn
        assert config.user_id_hmac_key == "a-vendor-secret-nobody-else-holds"

    def test_the_sink_the_dsn_built_does_not_print_its_bearer(self) -> None:
        """⚠ **Checked because the TypeScript sink DID leak here, not because
        this one looked suspicious.** The answer differs: ``HttpSink`` is a
        plain class, so the default ``repr`` names the type and an address and
        no fields at all, where a JS object enumerates them. Recorded as a
        measurement so "probably the same" does not get asked a third time.
        ``vars()`` and ``.api_key`` still reach it — both are someone asking
        for the credential by name, the same line this draws for ``Dsn.key``.
        """
        from baton.integrations._config import resolve_config
        from baton.integrations.official.install import VendorConfig

        resolved = resolve_config(
            VendorConfig(
                consent_token="ct",
                dsn=f"https://{KEY}@ingest.example.com/{WORKSPACE}/{SERVER}",
            )
        )
        sink = resolved.sink
        assert sink is not None
        assert KEY not in repr(sink)
        assert KEY not in str(sink)
        assert KEY not in f"{sink}"

    async def test_a_sink_whose_collector_is_GONE_does_not_log_the_bearer(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The realistic accidental-print path for a sink: it fails, something
        catches it, and the traceback goes to a log aggregator.

        Captured at DEBUG over EVERY logger, not just ours — ``httpx`` and
        ``httpcore`` trace each attempt, and a third-party library printing the
        request is as much of a leak as our own line would be. The host is
        asserted present first: it proves the sink actually dialled the DSN's
        origin, without which "the key is not in the log" is a statement about
        an empty log."""
        import logging
        from datetime import UTC, datetime

        from baton.events import ToolCallStartEvent, ToolCallStartPayload
        from baton.integrations._config import resolve_config
        from baton.integrations.official.install import VendorConfig
        from baton.sinks import safe_write

        resolved = resolve_config(
            VendorConfig(
                consent_token="ct",
                # Port 1 with nothing on it: a connection refused at the first
                # write, which is the shape a wrong origin actually takes.
                dsn=f"http://{KEY}@127.0.0.1:1/{WORKSPACE}/{SERVER}",
            )
        )
        sink = resolved.sink
        assert sink is not None
        with caplog.at_level(logging.DEBUG):
            event = ToolCallStartEvent(
                tenant_id=WORKSPACE,
                vendor_id=SERVER,
                session_id="sess_test",
                sequence_number=1,
                captured_at=datetime.now(UTC),
                consent_token="ct",
                agent_runtime="claude-code",
                payload=ToolCallStartPayload(tool_name="lookup"),
            )
            await safe_write(sink, event, logging.getLogger("baton"))
            await sink.flush()
            await sink.aclose()
        # ⚠ **Assert that something was checked.** "the key is not in an empty
        # log" is vacuously true, and this repo has already shipped a rig that
        # reported zero mispairs while matching nothing at all. The sink must
        # actually have tried and failed for the assertion below to mean
        # anything.
        assert "127.0.0.1" in caplog.text, "the sink never tried — the assertion below is vacuous"
        assert KEY not in caplog.text


class TestAnEmptyValueIsUnsetAtEVERYDoor:
    """⚠ **Falsy-means-unset was applied to ``select_dsn`` and to nothing
    else**, so the shape its own comment cites —
    ``os.environ.get("MY_DSN", "")`` — still died at three other checks. Found
    by review of that fix, each one reproduced before being changed.

    This file is where they belong: every one of them is two doors disagreeing
    about the same input, which is the class of defect this file exists for.
    """

    def test_an_empty_dsn_KWARG_beside_a_config_does_not_raise(self) -> None:
        """``build_config``'s conflict check was still ``dsn is not None``, so
        ``install_baton(mcp, my_config, dsn=os.environ.get("MY_DSN", ""))``
        raised — naming a ``dsn`` argument the vendor never filled in."""
        from baton.integrations._config import build_config
        from baton.integrations.official.install import VendorConfig

        config = VendorConfig(vendor_id="acme", vendor_display_name="Acme", consent_token="ct")
        assert build_config(config, "").vendor_id == "acme"

    def test_an_empty_tenant_id_beside_a_dsn_does_not_raise(self) -> None:
        """The ``supplied`` map read ``bool(vendor_id)`` on one line and
        ``tenant_id is not None`` on the next — one rule written two ways, one
        line apart."""
        from baton.integrations._config import resolve_config
        from baton.integrations.official.install import VendorConfig

        dsn = f"https://{KEY}@ingest.example.com/{WORKSPACE}/{SERVER}"
        resolved = resolve_config(VendorConfig(dsn=dsn, consent_token="ct", tenant_id=""))
        assert resolved.tenant_id == WORKSPACE

    def test_both_doors_agree_that_an_empty_vendor_id_is_unset(self) -> None:
        """The one that matters most here: ``Client(dsn=..., vendor_id="")``
        raised where the identical ``VendorConfig(dsn=..., vendor_id="")`` did
        not. Asserted as an AGREEMENT rather than as two separate cases,
        because a door drifting from its twin is what this file is for."""
        from baton.client import _resolve_client_config
        from baton.integrations._config import resolve_config
        from baton.integrations.official.install import VendorConfig

        dsn = f"https://{KEY}@ingest.example.com/{WORKSPACE}/{SERVER}"
        through_install = resolve_config(
            VendorConfig(dsn=dsn, consent_token="ct", vendor_id="", tenant_id="")
        )
        through_client = _resolve_client_config(
            sink=None, dsn=dsn, vendor_id="", tenant_id="", consent_token="ct"
        )
        assert through_install.vendor_id == through_client.vendor_id == SERVER
        assert through_install.tenant_id == through_client.tenant_id == WORKSPACE
