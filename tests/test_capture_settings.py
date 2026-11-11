"""Capture settings from the collector (SPEC §2.6) and the ``labels`` and
``capture`` envelope members (SPEC §11.4)."""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest
from fastmcp import Client, FastMCP

from baton import Organization, Principal, create_baton
from baton._capture_settings import (
    Caller,
    CaptureSettings,
    SettingsDocument,
    SettingsFetcher,
    clean_labels,
    parse_document,
    share_bucket,
)
from baton.events import CaptureObserved
from baton.integrations import _config as config_module
from baton.integrations import official, standalone
from baton.integrations._config import build_capture_settings
from baton.integrations._identity_adapter import _match_view, _wire_caller
from baton.integrations.official._compat import MCPServerClass as OfficialFastMCP
from baton.sinks import FileSink
from tests._event_helpers import read_events, without_surface_snapshots
from tests._mcp_session import connected_session

DSN = "https://baton_pk_x@h.example.com/ten_" + "0" * 32 + "/srv"
LOGGER = logging.getLogger("test")


def _document(*audiences: dict[str, Any], version: int = 3) -> bytes:
    return json.dumps({"version": version, "audiences": list(audiences)}).encode()


def _audience(audience_id: str, match: dict[str, Any], results: Any = "full") -> dict[str, Any]:
    return {
        "id": audience_id,
        "match": match,
        "capture": {"goal": "required", "reports": "off", "results": results},
    }


EVERYONE = _audience("everyone", {}, results="off")


def _parsed(body: bytes) -> SettingsDocument:
    document = parse_document(body)
    assert document is not None
    return document


def _matched(body: bytes, caller: Caller) -> str | None:
    return _parsed(body).settings_for(caller, "srv").capture.audience


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

    def test_an_organization_alone_can_carry_labels(self) -> None:
        caller = _wire_caller(Organization("org_1", labels=["enterprise"]), logger=LOGGER)
        assert caller.principal is None
        assert caller.envelope_fields()["labels"] == ["enterprise"]

    def test_a_share_samples_the_principal_and_never_the_organization(self) -> None:
        both = _wire_caller(Principal("user-1", organization=Organization("org_1")), logger=LOGGER)
        org_only = _wire_caller(Organization("org_1"), logger=LOGGER)
        assert _match_view(both, None).share_key == "user-1"
        assert _match_view(org_only, None).share_key is None

    @staticmethod
    def _then_raise() -> Any:
        yield "beta"
        raise RuntimeError("resolver bug")

    @staticmethod
    def _endless() -> Any:
        while True:
            yield "beta"

    def test_a_label_that_cannot_be_hashed_is_sent_as_plain_text(self) -> None:
        class Label(str):
            def __eq__(self, other: object) -> bool:
                return str.__eq__(self, other)

        caller = _wire_caller(Principal("u", labels=[Label("beta")]), logger=LOGGER)
        assert caller.labels == ("beta",)
        assert type(caller.labels[0]) is str

    @pytest.mark.parametrize("labels", [_then_raise, _endless])
    def test_labels_a_resolver_cannot_read_never_raise(self, labels: Any) -> None:
        caller = _wire_caller(Principal("u", labels=labels()), logger=LOGGER)
        assert caller.principal is not None
        assert len(caller.labels) <= 20


class TestShare:
    def test_the_bucket_is_pinned(self) -> None:
        # SPEC §2.6's formula; another SDK must give the same buckets.
        assert [share_bucket("acme", key) for key in ("alice", "bob", "carol")] == [
            1556,
            3902,
            7404,
        ]

    def test_a_half_rounds_up(self) -> None:
        # 0.00025 * 10000 is exactly 2.5, so buckets 0, 1 and 2 are in.
        key = next(k for i in itertools.count() if share_bucket("srv", k := f"u{i}") == 2)
        body = _document(_audience("tiny", {"share": 0.00025}), EVERYONE)
        assert _matched(body, Caller(share_key=key)) == "tiny"

    def test_raising_the_share_keeps_everyone_already_in(self) -> None:
        small = _document(_audience("in", {"share": 0.1}), EVERYONE)
        large = _document(_audience("in", {"share": 0.5}), EVERYONE)
        keys = [f"user-{i}" for i in range(500)]
        in_small = {k for k in keys if _matched(small, Caller(share_key=k)) == "in"}
        in_large = {k for k in keys if _matched(large, Caller(share_key=k)) == "in"}
        assert 25 < len(in_small) < 75
        assert in_small < in_large

    def test_share_needs_an_identity(self) -> None:
        body = _document(_audience("all", {"share": 1}), EVERYONE)
        assert _matched(body, Caller()) == "everyone"
        assert _matched(body, Caller(share_key="user-1")) == "all"


class TestDocument:
    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            b"[" * 100000,
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

    def test_the_first_matching_audience_wins(self) -> None:
        body = _document(
            _audience("beta", {"label": ["beta", "alpha"]}),
            _audience("claude", {"client": ["claude-code"]}),
            EVERYONE,
        )
        assert _matched(body, Caller(labels=("alpha",), client_name="claude-code")) == "beta"
        assert _matched(body, Caller(client_name="claude-code")) == "claude"
        assert _matched(body, Caller(labels=("gamma",))) == "everyone"

    def test_match_keys_combine_with_and(self) -> None:
        body = _document(_audience("both", {"label": ["beta"], "client": ["cursor"]}), EVERYONE)
        assert _matched(body, Caller(labels=("beta",))) == "everyone"
        assert _matched(body, Caller(labels=("beta",), client_name="cursor")) == "both"

    @pytest.mark.parametrize(
        "match",
        [
            {"organization": ["org_1"]},
            {"label": "beta"},
            {"label": [1]},
            {"client": "cursor"},
            {"share": "0.5"},
            {"share": True},
            {"share": 5},
            {"share": -1},
            {"share": 1e305},
            {"share": float("inf")},
            {"share": float("nan")},
            {"share": 10**400},
        ],
    )
    def test_an_audience_it_cannot_read_matches_nobody(self, match: dict[str, Any]) -> None:
        body = _document(_audience("odd", match), EVERYONE)
        assert _matched(body, Caller(labels=("beta",), client_name="cursor", share_key="u")) == (
            "everyone"
        )

    def test_the_matched_audience_sets_results_and_is_recorded(self) -> None:
        document = _parsed(_document(_audience("beta", {"label": ["beta"]}), EVERYONE, version=7))
        beta = document.settings_for(Caller(labels=("beta",)), "srv")
        assert beta.result_capture_mode == "full"
        assert beta.capture == CaptureObserved(settings_version=7, audience="beta")
        rest = document.settings_for(Caller(), "srv")
        assert rest.result_capture_mode == "off"
        assert rest.capture == CaptureObserved(settings_version=7, audience="everyone")

    @pytest.mark.parametrize("results", ["metadata", ["full"], {"a": 1}, None])
    def test_an_unknown_results_value_leaves_the_code_value(self, results: Any) -> None:
        document = _parsed(_document(_audience("everyone", {}, results=results)))
        assert document.settings_for(Caller(), "srv").result_capture_mode is None

    def test_no_matching_audience_records_the_version_alone(self) -> None:
        document = _parsed(_document(_audience("beta", {"label": ["beta"]})))
        settings = document.settings_for(Caller(), "srv")
        assert settings.result_capture_mode is None
        assert settings.capture == CaptureObserved(settings_version=3)


@dataclass
class _Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    data: bytes = b""


class _Pool:
    def __init__(self, *answers: _Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def urlopen(self, method: str, url: str, *, headers: dict[str, str], **_: Any) -> _Response:
        self.requests.append((method, url, headers))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _fetcher(pool: _Pool) -> SettingsFetcher:
    return SettingsFetcher(
        origin="https://h.example.com",
        api_key="baton_pk_x",
        tenant_id="ten_1",
        vendor_id="srv",
        refresh_s=60,
        make_pool=lambda: pool,
    )


class TestFetcher:
    def test_a_200_is_used_and_its_etag_sent_back(self) -> None:
        pool = _Pool(_Response(200, {"etag": '"v3-w1"'}, _document(EVERYONE)), _Response(304))
        fetcher = _fetcher(pool)
        fetcher.read_once()
        fetcher.read_once()
        assert fetcher.document is not None and fetcher.document.version == 3
        assert pool.requests[1][2]["If-None-Match"] == '"v3-w1"'

    def test_a_204_drops_the_document(self) -> None:
        fetcher = _fetcher(_Pool(_Response(200, data=_document(EVERYONE)), _Response(204)))
        fetcher.read_once()
        fetcher.read_once()
        assert fetcher.document is None

    @pytest.mark.parametrize(
        "failure",
        [_Response(500), _Response(401), _Response(200, data=b"{}"), OSError("down")],
    )
    def test_a_failure_keeps_the_document_held(self, failure: Any) -> None:
        fetcher = _fetcher(_Pool(_Response(200, data=_document(EVERYONE)), failure))
        fetcher.read_once()
        fetcher.read_once()
        assert fetcher.document is not None

    def test_a_forked_child_starts_its_own_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetcher = _fetcher(_Pool())
        started: list[int] = []
        monkeypatch.setattr(fetcher, "start", lambda: started.append(1))
        fetcher.reset_in_child()
        assert started == [1]
        fetcher.stop()
        fetcher.reset_in_child()
        assert started == [1]

    def test_a_started_read_registers_for_the_fork_hook(self) -> None:
        from baton.sinks import _LIVE_SENDERS

        fetcher = _fetcher(_Pool(_Response(204)))
        fetcher.start()
        fetcher.stop()
        assert fetcher in _LIVE_SENDERS


class TestConfig:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"settings": "cloud", "dsn": DSN}, "settings"),
            ({"settings": "console", "vendor_id": "srv", "vendor_display_name": "S"}, "dsn"),
            ({"settings_refresh_s": 5, "dsn": DSN}, "15"),
            ({"settings_refresh_s": float("nan"), "dsn": DSN}, "15"),
            ({"settings_refresh_s": float("inf"), "dsn": DSN}, "15"),
            ({"settings_refresh_s": "60", "dsn": DSN}, "15"),
            ({"settings_refresh_s": 1e10, "dsn": DSN}, "15"),
        ],
    )
    def test_bad_settings_are_refused_at_install(
        self, kwargs: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            official.install_baton(OfficialFastMCP("config"), official.VendorConfig(**kwargs))

    def test_local_settings_read_nothing(self) -> None:
        assert build_capture_settings(official.VendorConfig(dsn=DSN), "ten_1") is None

    def test_console_settings_read_with_the_dsns_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        given: list[dict[str, Any]] = []
        monkeypatch.setattr(config_module, "SettingsFetcher", lambda **kw: given.append(kw))
        config = config_module.resolve_config(
            official.VendorConfig(dsn=DSN, settings="console", settings_refresh_s=30)
        )
        assert build_capture_settings(config, "ten_1") is not None
        assert given == [
            {
                "origin": "https://h.example.com",
                "api_key": "baton_pk_x",
                "tenant_id": "ten_1",
                "vendor_id": "srv",
                "refresh_s": 30,
            }
        ]


class _Fetcher:
    def __init__(self, body: bytes | None) -> None:
        self.document = parse_document(body) if body is not None else None
        self.running = False
        self.starts = 0

    def start(self) -> None:
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False


class _Raising(CaptureSettings):
    def for_caller(self, caller: Caller) -> Any:
        raise RuntimeError("settings bug")


def _console() -> CaptureSettings:
    """A document already read, with no network."""
    body = _document(
        _audience("beta", {"label": ["beta"], "client": ["claude-code"]}, results="off"),
        EVERYONE,
        version=9,
    )
    return CaptureSettings(vendor_id="srv", fetcher=_Fetcher(body))  # type: ignore[arg-type]


def _resolver(context: Any) -> Principal:
    return Principal(
        "user-1",
        labels=["beta", "beta", ""],
        organization=Organization("org_1", labels=("enterprise",)),
    )


def _config(module: Any, events_path: Path) -> Any:
    return module.VendorConfig(
        vendor_id="srv",
        vendor_display_name="Server",
        consent_token="ct",
        tenant_id="ten_1",
        sink=FileSink(str(events_path)),
        resolve_principal=_resolver,
    )


Settings = Callable[[], CaptureSettings] | None


def _use_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    from baton.integrations import _shared

    if settings is not None:
        monkeypatch.setattr(_shared, "build_capture_settings", lambda *_: settings())


async def _drive_official(
    events_path: Path, monkeypatch: pytest.MonkeyPatch, client: str, settings: Settings
) -> None:
    _use_settings(monkeypatch, settings)
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
    events_path: Path, monkeypatch: pytest.MonkeyPatch, client: str, settings: Settings
) -> None:
    _use_settings(monkeypatch, settings)
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


async def _drive_create_baton(
    events_path: Path, monkeypatch: pytest.MonkeyPatch, client: str, settings: Settings
) -> None:
    _use_settings(monkeypatch, settings)
    baton = create_baton(_config(official, events_path))
    try:
        for _ in range(2):
            mcp = OfficialFastMCP("settings-per-request")

            @mcp.tool()
            def lookup(name: str) -> dict[str, Any]:
                return {"name": name}

            baton.wrap(mcp)
        async with connected_session(mcp, declared_name=client) as session:
            await session.list_tools()
            await session.call_tool("lookup", {"name": "alice", "user_goal": "find alice"})
    finally:
        await baton.aclose()


DRIVERS = pytest.mark.parametrize(
    "drive", [_drive_official, _drive_standalone, _drive_create_baton]
)


def _end(events_path: Path) -> dict[str, Any]:
    (end,) = [e for e in read_events(events_path) if e["event_type"] == "tool_call_end"]
    return end


@DRIVERS
async def test_events_carry_the_callers_labels_and_audience(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "e.jsonl"
    await drive(events_path, monkeypatch, "claude-code", _console)
    events = without_surface_snapshots(read_events(events_path))
    assert {"tool_call_start", "tool_call_end", "tool_list_end"} <= {
        e["event_type"] for e in events
    }
    for event in events:
        assert event["labels"] == ["beta", "enterprise"], event["event_type"]
        assert event["capture"] == {"settings_version": 9, "audience": "beta"}, event["event_type"]
    end = _end(events_path)
    assert end["payload"]["result_capture"] == "off"
    assert end["payload"]["result"] is None


@DRIVERS
async def test_a_caller_outside_the_audience_gets_everyones_settings(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "e.jsonl"
    await drive(events_path, monkeypatch, "cursor", _console)
    end = _end(events_path)
    assert end["capture"] == {"settings_version": 9, "audience": "everyone"}
    assert end["payload"]["result_capture"] == "off"


@DRIVERS
async def test_code_settings_send_labels_and_no_capture(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "e.jsonl"
    await drive(events_path, monkeypatch, "claude-code", None)
    end = _end(events_path)
    assert end["labels"] == ["beta", "enterprise"]
    assert end["capture"] is None
    assert end["payload"]["result"] is not None


@DRIVERS
async def test_settings_that_fail_to_apply_still_serve_the_call(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "e.jsonl"
    raising = _Raising(vendor_id="srv", fetcher=_Fetcher(None))  # type: ignore[arg-type]
    await drive(events_path, monkeypatch, "claude-code", lambda: raising)
    assert not [e for e in read_events(events_path) if e["event_type"] == "tool_call_error"]
    end = _end(events_path)
    assert end["capture"] is None
    assert end["labels"] == ["beta", "enterprise"]


@DRIVERS
async def test_one_read_starts_at_install_and_stops_at_close(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _Fetcher(None)
    settings = CaptureSettings(vendor_id="srv", fetcher=fetcher)  # type: ignore[arg-type]
    await drive(tmp_path / "e.jsonl", monkeypatch, "claude-code", lambda: settings)
    assert fetcher.starts == 1
    assert not fetcher.running


@pytest.mark.parametrize("adapter", [official, standalone])
def test_a_failed_install_reads_no_settings(
    adapter: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _Fetcher(None)
    settings = CaptureSettings(vendor_id="srv", fetcher=fetcher)  # type: ignore[arg-type]
    _use_settings(monkeypatch, lambda: settings)

    def failing_attach(*_: Any) -> Any:
        raise RuntimeError("attach failed")

    monkeypatch.setattr(adapter.install, "attach", failing_attach)
    server = OfficialFastMCP("failed") if adapter is official else FastMCP("failed")
    with pytest.raises(RuntimeError, match="attach failed"):
        adapter.install_baton(server, _config(adapter, tmp_path / "e.jsonl"))
    assert fetcher.starts == 0


def test_a_real_request_reads_the_document_and_its_etag() -> None:
    import http.server
    import threading

    seen: list[dict[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append({"path": self.path, **dict(self.headers.items())})
            if self.headers.get("If-None-Match") == '"v3-w1"':
                self.send_response(304)
                self.end_headers()
                return
            body = _document(EVERYONE)
            self.send_response(200)
            self.send_header("ETag", '"v3-w1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fetcher = SettingsFetcher(
            origin=f"http://127.0.0.1:{server.server_port}",
            api_key="baton_pk_x",
            tenant_id="ten_1",
            vendor_id="srv",
            refresh_s=60,
        )
        fetcher.read_once()
        fetcher.read_once()
    finally:
        server.shutdown()
    assert fetcher.document is not None and fetcher.document.version == 3
    assert [r["path"] for r in seen] == ["/v0/settings?server=srv"] * 2
    assert seen[0]["Authorization"] == "Bearer baton_pk_x"
    assert seen[0]["X-Baton-Tenant-Id"] == "ten_1"
    assert seen[1]["If-None-Match"] == '"v3-w1"'
