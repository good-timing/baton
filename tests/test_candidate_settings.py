"""Test changes fetched by id: ``BATON_CANDIDATE`` and the ``Baton-Candidate``
header (SPEC §2.6), and ``capture.candidate`` on events (SPEC §11.4)."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from baton import Client as LibraryClient
from baton._capture_settings import (
    _PENDING,
    CANDIDATE_READS_AT_ONCE,
    CANDIDATES_HELD,
    Caller,
    CaptureSettings,
    header_candidate,
    may_hide,
    parse_document,
)
from baton.integrations import _config as config_module
from baton.integrations import standalone
from baton.sinks import FileSink
from tests._candidate_helpers import (
    CHANGE,
    CHANGED,
    TODAY,
    Collector,
    CollectorResponse,
    candidate_settings,
    document,
    started,
    wait_for,
)
from tests._event_helpers import read_events

DSN = "https://baton_pk_x@h.example.com/ten_" + "0" * 32 + "/srv"


def _served(settings: CaptureSettings, candidate: str | None) -> tuple[str | None, str | None]:
    """The description of ``lookup`` and the event tag a request gets."""
    call = settings.for_caller(Caller(candidate=candidate))
    assert call is not None and call.overlay is not None
    return call.overlay.actions[0].text, call.capture.candidate


def _settled(settings: CaptureSettings, candidate: str) -> tuple[str | None, str | None]:
    """What a request with the header gets once the background read is done."""
    _served(settings, candidate)
    wait_for(lambda: candidate not in _pending(settings))
    return _served(settings, candidate)


def _pending(settings: CaptureSettings) -> set[str]:
    return {k for k, v in settings._held.items() if v is _PENDING}


class TestHeader:
    def test_a_test_key_gets_the_changes_text_and_its_tag(self) -> None:
        settings = started(Collector({CHANGE: CHANGED}))
        assert _settled(settings, CHANGE) == ("The change's text.", CHANGE)

    def test_the_first_request_gets_todays_settings_untagged(self) -> None:
        settings = started(Collector({CHANGE: CHANGED}))
        assert _served(settings, CHANGE) == ("Today's text.", None)

    def test_a_prod_key_gets_todays_settings_and_one_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        collector = Collector({"a": 403, "b": 403})
        settings = started(collector)
        assert _settled(settings, "a") == ("Today's text.", None)
        assert _served(settings, "b") == ("Today's text.", None)
        assert collector.change_reads() == ["a"]
        assert len([r for r in caplog.records if "not a test key" in r.message]) == 1

    @pytest.mark.parametrize("answer", [404, 500, OSError("down"), {"version": "bad"}])
    def test_a_change_that_cannot_be_read_gets_todays_settings(self, answer: Any) -> None:
        settings = started(Collector({CHANGE: answer}))
        assert _settled(settings, CHANGE) == ("Today's text.", None)

    def test_a_document_for_another_change_is_not_served(self) -> None:
        settings = started(Collector({CHANGE: document("Other.", candidate="chg_2")}))
        assert _settled(settings, CHANGE) == ("Today's text.", None)

    def test_an_unknown_id_is_read_once(self) -> None:
        collector = Collector({CHANGE: 404})
        settings = started(collector)
        _settled(settings, CHANGE)
        _served(settings, CHANGE)
        assert collector.change_reads() == [CHANGE]

    def test_a_failed_read_is_tried_again_after_a_wait(self) -> None:
        collector = Collector({CHANGE: OSError("down")})
        settings = started(collector)
        settings.retry_after_s = 0.2
        _settled(settings, CHANGE)
        collector.changes[CHANGE] = CHANGED
        assert _settled(settings, CHANGE) == ("Today's text.", None)
        assert collector.change_reads() == [CHANGE]
        time.sleep(0.25)
        _settled(settings, CHANGE)
        assert _settled(settings, CHANGE) == ("The change's text.", CHANGE)

    def test_a_read_whose_thread_cannot_start_is_not_left_reading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = started(Collector({CHANGE: CHANGED}))

        def refuse(self: Any) -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", refuse)
        assert _served(settings, CHANGE) == ("Today's text.", None)
        assert not _pending(settings)

    def test_a_forked_child_forgets_reads_in_flight(self) -> None:
        settings = started(Collector({CHANGE: CHANGED}))
        settings._held[CHANGE] = _PENDING
        settings.reset_in_child()
        assert not _pending(settings)

    def test_at_most_32_ids_are_held(self) -> None:
        ids = [f"chg_{i}" for i in range(CANDIDATES_HELD + 1)]
        collector = Collector({i: document(i, candidate=i) for i in ids})
        settings = started(collector)
        for i in ids:
            _settled(settings, i)
        assert len(settings._held) == CANDIDATES_HELD
        assert _served(settings, ids[0]) == ("Today's text.", None)
        assert collector.change_reads().count(ids[0]) == 2

    def test_at_most_four_reads_run_at_once(self) -> None:
        release = threading.Event()

        class Slow(Collector):
            def urlopen(self, method: str, url: str, **kwargs: Any) -> CollectorResponse:
                if "candidate=" in url:
                    release.wait(5)
                return super().urlopen(method, url, **kwargs)

        ids = [f"chg_{i}" for i in range(CANDIDATE_READS_AT_ONCE + 2)]
        collector = Slow({i: document(i, candidate=i) for i in ids})
        settings = started(collector)
        for i in ids:
            assert _served(settings, i) == ("Today's text.", None)
        assert len(_pending(settings)) == CANDIDATE_READS_AT_ONCE
        release.set()
        wait_for(lambda: not _pending(settings))
        assert _settled(settings, ids[-1]) == (ids[-1], ids[-1])

    @pytest.mark.parametrize("value", ["", "a b", "x" * 129, "../settings", "é"])
    def test_a_header_that_cannot_be_an_id_is_ignored(self, value: str) -> None:
        assert header_candidate({"Baton-Candidate": value}) is None

    def test_the_header_is_read_from_each_adapters_mapping(self) -> None:
        from tests._asgi import starlette_headers

        assert header_candidate({"baton-candidate": f" {CHANGE} "}) == CHANGE
        assert header_candidate(starlette_headers({"Baton-Candidate": CHANGE})) == CHANGE

    def test_a_tool_the_change_hides_may_be_hidden(self) -> None:
        hides = document("x", candidate=CHANGE)
        hides["overlays"]["ov_1"]["actions"].append({"target": {"tool": "drop"}, "remove": True})
        settings = started(Collector({CHANGE: hides}))
        assert not may_hide(settings, "drop")
        _settled(settings, CHANGE)
        assert may_hide(settings, "drop")


class TestVariable:
    def test_a_test_key_serves_the_change_from_the_start(self) -> None:
        settings = candidate_settings(Collector({CHANGE: CHANGED}), CHANGE)
        settings.start()
        assert _served(settings, None) == ("The change's text.", CHANGE)

    def test_the_header_wins(self) -> None:
        other = document("Another change.", candidate="chg_2")
        settings = candidate_settings(Collector({CHANGE: CHANGED, "chg_2": other}), CHANGE)
        settings.start()
        assert _settled(settings, "chg_2") == ("Another change.", "chg_2")

    def test_a_header_whose_change_is_not_held_gets_todays_settings(self) -> None:
        settings = candidate_settings(Collector({CHANGE: CHANGED, "chg_2": 404}), CHANGE)
        settings.start()
        wait_for(lambda: settings._fetcher.document is not None)
        assert _settled(settings, "chg_2") == ("Today's text.", None)

    def test_a_header_retries_the_variables_failed_read(self) -> None:
        collector = Collector({CHANGE: OSError("down")})
        settings = candidate_settings(collector, CHANGE)
        settings.retry_after_s = 0
        settings.start()
        wait_for(lambda: settings.document is not None)
        collector.changes[CHANGE] = CHANGED
        assert _settled(settings, CHANGE) == ("The change's text.", CHANGE)

    def test_a_prod_key_warns_once_and_serves_todays_settings(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = started(Collector({CHANGE: 403, "b": 403}), CHANGE)
        assert _served(settings, None) == ("Today's text.", None)
        assert _served(settings, "b") == ("Today's text.", None)
        warned = [r for r in caplog.records if "not a test key" in r.message]
        assert [r.levelno for r in warned] == [logging.WARNING]

    @pytest.mark.parametrize("answer", [404, OSError("timed out")])
    def test_a_change_that_cannot_be_read_logs_an_error_and_serves_today(
        self, answer: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = started(Collector({CHANGE: answer}), CHANGE)
        assert _served(settings, None) == ("Today's text.", None)
        assert any(
            r.levelno == logging.ERROR and "BATON_CANDIDATE" in r.message for r in caplog.records
        )

    def test_a_read_that_hangs_holds_up_the_start_for_the_timeout_only(self) -> None:
        timeouts: list[float] = []

        class Hanging(Collector):
            def urlopen(self, method: str, url: str, **kwargs: Any) -> CollectorResponse:
                if "candidate=" in url:
                    timeouts.append(kwargs["timeout"].total)
                    raise TimeoutError
                return super().urlopen(method, url, **kwargs)

        settings = candidate_settings(Hanging({}), CHANGE)
        settings.start()
        assert timeouts == [0.5]
        wait_for(lambda: settings.document is not None)
        assert _served(settings, None) == ("Today's text.", None)

    @pytest.mark.parametrize("install", ["mcp", "library"])
    async def test_local_settings_log_one_error(
        self, install: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("BATON_CANDIDATE", CHANGE)
        if install == "mcp":
            handle = standalone.install_baton(FastMCP("local"), standalone.VendorConfig(dsn=DSN))
            await handle.aclose()
        else:
            LibraryClient(dsn=DSN).close()
        errors = [r for r in caplog.records if "not 'console'" in r.message]
        assert [r.levelno for r in errors] == [logging.ERROR]

    def test_console_settings_read_the_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BATON_CANDIDATE", CHANGE)
        config = config_module.resolve_config(standalone.VendorConfig(dsn=DSN, settings="console"))
        settings = config_module.build_capture_settings(config, "ten_1")
        assert settings is not None and settings._candidate == CHANGE


def test_a_document_without_the_changes_id_is_not_valid() -> None:
    assert parse_document(json.dumps(TODAY).encode(), CHANGE) is None
    assert parse_document(json.dumps(CHANGED).encode(), CHANGE) is not None


def _use(monkeypatch: pytest.MonkeyPatch, settings: CaptureSettings) -> None:
    from baton.integrations import _shared

    monkeypatch.setattr(_shared, "build_capture_settings", lambda *_: settings)


def _server(events: Path) -> Any:
    mcp: Any = FastMCP("candidate", instructions="Vendor text.")

    @mcp.tool
    def lookup(name: str) -> str:
        """Vendor description."""
        return name

    handle = standalone.install_baton(
        mcp,
        standalone.VendorConfig(
            vendor_id="srv",
            vendor_display_name="Server",
            consent_token="ct",
            tenant_id="ten_1",
            sink=FileSink(str(events)),
        ),
    )
    return mcp, handle


async def test_the_variables_change_is_in_place_before_the_first_initialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_overlay = {"actions": [{"target": {"instructions": True}, "append": "Changed."}]}
    changed = document(
        "The change's text.",
        candidate=CHANGE,
        server_overlay="ov_s",
    )
    changed["overlays"]["ov_s"] = server_overlay
    _use(monkeypatch, candidate_settings(Collector({CHANGE: changed}), CHANGE))
    events = tmp_path / "e.jsonl"
    mcp, handle = _server(events)
    # Read before any connection: on stdio the one ``initialize`` comes first.
    instructions = mcp.instructions
    try:
        async with Client(mcp) as client:
            (tool,) = await client.list_tools()
            await client.call_tool("lookup", {"name": "a", "user_goal": "1: find a"})
    finally:
        await handle.aclose()
    assert instructions is not None and "Changed." in instructions
    assert tool.description == "The change's text."
    (end,) = [e for e in read_events(events) if e["event_type"] == "tool_call_end"]
    assert end["capture"]["candidate"] == CHANGE


def test_the_library_path_takes_the_header(tmp_path: Path) -> None:
    from baton._library_session import session_for_call

    settings = started(Collector({CHANGE: CHANGED}))
    client = LibraryClient(sink=FileSink(str(tmp_path / "e.jsonl")), vendor_id="srv")
    headers = {"Baton-Candidate": CHANGE}
    try:
        with client.session(headers=headers):
            session_for_call(client, settings)
        wait_for(lambda: CHANGE not in _pending(settings))
        with client.session(headers=headers):
            caller = session_for_call(client, settings).caller
        assert caller.settings is not None
        assert caller.settings.capture.candidate == CHANGE
    finally:
        client.close()


def test_the_limits_are_the_specs() -> None:
    from baton._capture_settings import CANDIDATE_START_TIMEOUT_S, candidate_id

    assert CANDIDATES_HELD == 32
    assert CANDIDATE_READS_AT_ONCE == 4
    assert CANDIDATE_START_TIMEOUT_S == 5.0
    assert candidate_id("x" * 128) == "x" * 128


@pytest.mark.parametrize("overlays", [None, {"ov_1": {"actions": []}}])
def test_a_change_with_no_actions_still_tags_its_events(overlays: Any) -> None:
    baseline = document("unused", candidate=CHANGE)
    if overlays is None:
        del baseline["overlays"], baseline["audiences"][0]["overlay"]
    else:
        baseline["overlays"] = overlays
    settings = started(Collector({CHANGE: baseline}))
    _served(settings, CHANGE)
    wait_for(lambda: CHANGE not in _pending(settings))
    call = settings.for_caller(Caller(candidate=CHANGE))
    assert call is not None and call.capture.candidate == CHANGE


def test_the_pool_holds_the_poll_and_every_change_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import baton._capture_settings as module
    from baton._capture_settings import SettingsFetcher

    sizes: list[int] = []
    monkeypatch.setattr(module, "_pool_through", lambda _proxy, maxsize: sizes.append(maxsize))
    fetcher = SettingsFetcher(
        origin="https://h.example.com", api_key="k", tenant_id="t", vendor_id="srv", refresh_s=60
    )
    fetcher._make_pool()
    assert sizes == [5]
