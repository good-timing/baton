"""Capture settings from the collector (SPEC §2.6) and the ``labels`` and
``capture`` envelope members (SPEC §11.4)."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest
from fastmcp import Client, FastMCP

from baton import Organization, Principal
from baton._capture_settings import (
    Caller,
    CaptureSettings,
    SettingsFetcher,
    clean_labels,
    parse_document,
    share_bucket,
)
from baton.events import CaptureObserved
from baton.integrations import _config as config_module
from baton.integrations import official, standalone
from baton.integrations._config import build_capture_settings
from baton.integrations.official._compat import MCPServerClass as OfficialFastMCP
from baton.sinks import FileSink
from tests._event_helpers import read_events, without_surface_snapshots
from tests._mcp_session import connected_session

DSN = "https://baton_pk_x@h.example.com/ten_" + "0" * 32 + "/srv"


def _document(*audiences: dict[str, Any], version: int = 3) -> bytes:
    return json.dumps({"version": version, "audiences": list(audiences)}).encode()


def _audience(audience_id: str, match: dict[str, Any], results: str = "full") -> dict[str, Any]:
    return {
        "id": audience_id,
        "match": match,
        "capture": {"goal": "required", "reports": "off", "results": results},
    }


EVERYONE = _audience("everyone", {}, results="off")


class TestLabels:
    def test_usable_labels_are_kept_in_order_without_repeats(self) -> None:
        assert clean_labels(["beta", "org_1", "beta"]) == ("beta", "org_1")

    @pytest.mark.parametrize("bad", [None, 3, "", "  ", chr(0xA0), "x" * 65, "a\x00b", "a\ud800b"])
    def test_an_unusable_label_is_dropped_alone(self, bad: Any) -> None:
        assert clean_labels(["beta", bad]) == ("beta",)

    def test_a_label_of_64_characters_is_kept(self) -> None:
        assert clean_labels(["x" * 64]) == ("x" * 64,)

    def test_at_most_twenty_are_kept(self) -> None:
        assert clean_labels([f"l{i}" for i in range(25)]) == tuple(f"l{i}" for i in range(20))


class TestShare:
    def test_the_bucket_is_pinned(self) -> None:
        # SPEC §2.6's formula; another SDK must give the same buckets.
        assert [share_bucket("acme", key) for key in ("alice", "bob", "carol")] == [
            1556,
            3902,
            7404,
        ]

    def test_raising_the_share_keeps_everyone_already_in(self) -> None:
        keys = [f"user-{i}" for i in range(500)]
        small = {k for k in keys if share_bucket("srv", k) < 1000}
        large = {k for k in keys if share_bucket("srv", k) < 5000}
        assert small and small <= large

    def test_the_share_is_close_to_what_was_asked(self) -> None:
        keys = [f"user-{i}" for i in range(5000)]
        inside = sum(share_bucket("srv", k) < 1000 for k in keys)
        assert 400 < inside < 600


class TestDocument:
    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            _document(version=0),
            json.dumps({"version": True, "audiences": [EVERYONE]}).encode(),
            json.dumps({"version": 1, "audiences": []}).encode(),
            json.dumps(
                {"version": 1, "audiences": [{"id": 1, "match": {}, "capture": {}}]}
            ).encode(),
            json.dumps(
                {"version": 1, "audiences": [{"id": "a", "match": [], "capture": {}}]}
            ).encode(),
        ],
    )
    def test_an_invalid_document_is_refused(self, body: bytes) -> None:
        assert parse_document(body) is None

    def _audience_for(self, body: bytes, caller: Caller) -> str | None:
        document = parse_document(body)
        assert document is not None
        audience = document.audience_for(caller, "srv")
        return audience.id if audience is not None else None

    def test_the_first_matching_audience_wins(self) -> None:
        body = _document(
            _audience("beta", {"label": ["beta", "alpha"]}),
            _audience("claude", {"client": ["claude-code"]}),
            EVERYONE,
        )
        both = Caller(labels=("alpha",), client_name="claude-code")
        assert self._audience_for(body, both) == "beta"
        assert self._audience_for(body, Caller(client_name="claude-code")) == "claude"
        assert self._audience_for(body, Caller(labels=("gamma",))) == "everyone"

    def test_match_keys_combine_with_and(self) -> None:
        body = _document(_audience("both", {"label": ["beta"], "client": ["cursor"]}), EVERYONE)
        assert self._audience_for(body, Caller(labels=("beta",))) == "everyone"
        assert self._audience_for(body, Caller(labels=("beta",), client_name="cursor")) == "both"

    def test_share_needs_an_identity(self) -> None:
        body = _document(_audience("all", {"share": 1}), EVERYONE)
        assert self._audience_for(body, Caller()) == "everyone"
        assert self._audience_for(body, Caller(share_key="user-1")) == "all"

    @pytest.mark.parametrize(
        "match",
        [{"organization": ["org_1"]}, {"label": "beta"}, {"client": "cursor"}, {"share": "0.5"}],
    )
    def test_an_audience_it_cannot_read_matches_nobody(self, match: dict[str, Any]) -> None:
        body = _document(_audience("odd", match), EVERYONE)
        caller = Caller(labels=("beta",), client_name="cursor", share_key="u")
        assert self._audience_for(body, caller) == "everyone"


class _Fetcher:
    def __init__(self, body: bytes | None) -> None:
        self.document = parse_document(body) if body is not None else None
        self.running = False
        self.started = False

    def start(self) -> None:
        self.running = self.started = True

    def stop(self) -> None:
        self.running = False


class TestForCaller:
    def _settings(self, body: bytes | None) -> CaptureSettings:
        return CaptureSettings(
            vendor_id="srv",
            result_capture_mode="full",
            fetcher=_Fetcher(body),  # type: ignore[arg-type]
        )

    def test_without_a_document_the_code_values_apply_and_nothing_is_recorded(self) -> None:
        settings = self._settings(None).for_caller(Caller())
        assert settings.result_capture_mode == "full"
        assert settings.capture is None

    def test_the_matched_audience_sets_results_and_is_recorded(self) -> None:
        body = _document(_audience("beta", {"label": ["beta"]}), EVERYONE, version=7)
        settings = self._settings(body)
        beta = settings.for_caller(Caller(labels=("beta",)))
        assert beta.result_capture_mode == "full"
        assert beta.capture == CaptureObserved(settings_version=7, audience="beta")
        rest = settings.for_caller(Caller())
        assert rest.result_capture_mode == "off"
        assert rest.capture == CaptureObserved(settings_version=7, audience="everyone")

    def test_an_unknown_results_value_falls_back_to_the_code_value(self) -> None:
        body = _document(_audience("everyone", {}, results="metadata"))
        assert self._settings(body).for_caller(Caller()).result_capture_mode == "full"

    def test_no_matching_audience_records_the_version_alone(self) -> None:
        body = _document(_audience("beta", {"label": ["beta"]}))
        settings = self._settings(body).for_caller(Caller())
        assert settings.result_capture_mode == "full"
        assert settings.capture == CaptureObserved(settings_version=3)


class _Opener:
    def __init__(self, *answers: tuple[int, dict[str, str], bytes] | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[urllib.request.Request] = []

    def __call__(
        self, request: urllib.request.Request, timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fetcher(opener: _Opener) -> SettingsFetcher:
    return SettingsFetcher(
        origin="https://h.example.com",
        api_key="baton_pk_x",
        tenant_id="ten_1",
        vendor_id="srv",
        opener=opener,
    )


class TestFetcher:
    def test_the_request_names_the_server_and_carries_the_key(self) -> None:
        opener = _Opener((204, {}, b""))
        _fetcher(opener).read_once()
        (request,) = opener.requests
        assert request.full_url == "https://h.example.com/v0/settings?server=srv"
        assert request.get_header("Authorization") == "Bearer baton_pk_x"
        assert request.get_header("X-baton-tenant-id") == "ten_1"

    def test_a_200_is_used_and_its_etag_sent_back(self) -> None:
        opener = _Opener((200, {"etag": '"v3-w1"'}, _document(EVERYONE)), (304, {}, b""))
        fetcher = _fetcher(opener)
        fetcher.read_once()
        fetcher.read_once()
        assert fetcher.document is not None and fetcher.document.version == 3
        assert opener.requests[1].get_header("If-none-match") == '"v3-w1"'

    def test_a_204_drops_the_document(self) -> None:
        fetcher = _fetcher(_Opener((200, {}, _document(EVERYONE)), (204, {}, b"")))
        fetcher.read_once()
        fetcher.read_once()
        assert fetcher.document is None

    @pytest.mark.parametrize(
        "failure",
        [(500, {}, b""), (401, {}, b""), (200, {}, b"{}"), OSError("down")],
    )
    def test_a_failure_keeps_the_document_held(self, failure: Any) -> None:
        fetcher = _fetcher(_Opener((200, {}, _document(EVERYONE)), failure))
        fetcher.read_once()
        fetcher.read_once()
        assert fetcher.document is not None


class TestConfig:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"settings": "cloud", "dsn": DSN}, "settings"),
            ({"settings": "console", "vendor_id": "srv", "vendor_display_name": "S"}, "dsn"),
            ({"settings_refresh_s": 5, "dsn": DSN}, "15"),
        ],
    )
    def test_bad_settings_are_refused_at_install(
        self, kwargs: dict[str, Any], message: str
    ) -> None:
        mcp = OfficialFastMCP("config")
        with pytest.raises(ValueError, match=message):
            official.install_baton(mcp, official.VendorConfig(**kwargs))

    def test_local_settings_read_nothing(self) -> None:
        assert build_capture_settings(official.VendorConfig(dsn=DSN), "ten_1") is None

    def test_console_settings_read_with_the_dsns_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        opener = _Opener((204, {}, b""))
        built: list[SettingsFetcher] = []

        def fetcher(**kwargs: Any) -> SettingsFetcher:
            built.append(SettingsFetcher(**kwargs, opener=opener))
            return built[-1]

        monkeypatch.setattr(config_module, "SettingsFetcher", fetcher)
        config = config_module.resolve_config(official.VendorConfig(dsn=DSN, settings="console"))
        assert build_capture_settings(config, "ten_" + "0" * 32) is not None
        built[0].read_once()
        (request,) = opener.requests
        assert request.full_url == "https://h.example.com/v0/settings?server=srv"
        assert request.get_header("Authorization") == "Bearer baton_pk_x"
        assert request.get_header("X-baton-tenant-id") == "ten_" + "0" * 32


def _resolver(context: Any) -> Principal:
    return Principal(
        "user-1",
        labels=["beta", "beta", ""],
        organization=Organization("org_1", labels=("enterprise",)),
    )


def _console_settings(results: str = "off") -> CaptureSettings:
    """A document already read, with no network."""
    body = _document(
        _audience("beta", {"label": ["beta"], "client": ["claude-code"]}, results=results),
        EVERYONE,
        version=9,
    )
    return CaptureSettings(
        vendor_id="srv",
        result_capture_mode="full",
        fetcher=_Fetcher(body),  # type: ignore[arg-type]
    )


def _config(module: Any, events_path: Path, **kwargs: Any) -> Any:
    return module.VendorConfig(
        vendor_id="srv",
        vendor_display_name="Server",
        consent_token="ct",
        tenant_id="ten_1",
        sink=FileSink(str(events_path)),
        resolve_principal=_resolver,
        **kwargs,
    )


async def _drive_official(events_path: Path, monkeypatch: pytest.MonkeyPatch, client: str) -> None:
    from baton.integrations.official import install

    monkeypatch.setattr(install, "build_capture_settings", lambda *_: _console_settings())
    mcp = OfficialFastMCP("settings-official")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"name": name}

    handle = official.install_baton(mcp, _config(official, events_path))
    try:
        async with connected_session(mcp, declared_name=client) as session:
            await session.list_tools()
            await session.call_tool("lookup", {"name": "alice", "user_goal": "find alice"})
    finally:
        await handle.aclose()


async def _drive_standalone(
    events_path: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    from baton.integrations.standalone import install

    monkeypatch.setattr(install, "build_capture_settings", lambda *_: _console_settings())
    mcp: Any = FastMCP("settings-standalone")

    @mcp.tool
    def lookup(name: str) -> dict[str, Any]:
        return {"name": name}

    handle = standalone.install_baton(mcp, _config(standalone, events_path))
    try:
        declared = mcp_types.Implementation(name=client, version="1.0")
        async with Client(mcp, client_info=declared) as session:
            await session.list_tools()
            await session.call_tool("lookup", {"name": "alice", "user_goal": "find alice"})
    finally:
        await handle.aclose()


DRIVERS = pytest.mark.parametrize("drive", [_drive_official, _drive_standalone])


@DRIVERS
async def test_events_carry_the_callers_labels_and_audience(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "e.jsonl"
    await drive(events_path, monkeypatch, "claude-code")
    events = without_surface_snapshots(read_events(events_path))
    types = {e["event_type"] for e in events}
    assert {"tool_call_start", "tool_call_end", "tool_list_end"} <= types
    for event in events:
        assert event["labels"] == ["beta", "enterprise"], event["event_type"]
        assert event["capture"] == {"settings_version": 9, "audience": "beta"}, event["event_type"]
    (end,) = [e for e in events if e["event_type"] == "tool_call_end"]
    assert end["payload"]["result_capture"] == "off"
    assert end["payload"]["result"] is None


@DRIVERS
async def test_a_caller_outside_the_audience_gets_everyones_settings(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "e.jsonl"
    await drive(events_path, monkeypatch, "cursor")
    (end,) = [e for e in read_events(events_path) if e["event_type"] == "tool_call_end"]
    assert end["capture"] == {"settings_version": 9, "audience": "everyone"}
    assert end["payload"]["result_capture"] == "off"


async def test_code_settings_send_labels_and_no_capture(tmp_path: Path) -> None:
    events_path = tmp_path / "e.jsonl"
    mcp = OfficialFastMCP("settings-local")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"name": name}

    handle = official.install_baton(mcp, _config(official, events_path))
    try:
        async with connected_session(mcp) as session:
            await session.call_tool("lookup", {"name": "alice", "user_goal": "find alice"})
    finally:
        await handle.aclose()
    (end,) = [e for e in read_events(events_path) if e["event_type"] == "tool_call_end"]
    assert end["labels"] == ["beta", "enterprise"]
    assert end["capture"] is None
    assert end["payload"]["result"] is not None


def test_an_organization_alone_can_carry_labels() -> None:
    from baton.integrations.identity_adapter import _wire_caller

    caller = _wire_caller(
        Organization("org_1", labels=["enterprise"]), logger=__import__("logging").getLogger()
    )
    assert caller.principal is None
    assert caller.wire_labels == ["enterprise"]


@DRIVERS
async def test_the_read_starts_at_install_and_stops_at_close(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetchers: list[_Fetcher] = []
    real = _console_settings

    def recording() -> CaptureSettings:
        settings = real()
        fetchers.append(settings._fetcher)  # type: ignore[arg-type]
        return settings

    monkeypatch.setattr(sys.modules[__name__], "_console_settings", recording)
    await drive(tmp_path / "e.jsonl", monkeypatch, "claude-code")
    (fetcher,) = fetchers
    assert fetcher.started
    assert not fetcher.running
