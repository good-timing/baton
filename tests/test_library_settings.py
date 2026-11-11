"""Console settings on the library API (SPEC §2.6, §5.6)."""

from __future__ import annotations

import asyncio
import gc
import json
from typing import Any

import pytest

from baton import AsyncClient, Client, Organization, Principal
from baton._capture_settings import CaptureSettings, parse_document
from baton.events import Event
from baton.sinks import Sink

DSN = "https://key@example.invalid/ten_0123abcd/srv_lib"
DOCUMENT = {
    "version": 4,
    "audiences": [
        {"id": "enterprise", "match": {"label": ["enterprise"]}, "capture": {"results": "off"}},
        {"id": "everyone", "match": {}, "capture": {}},
    ],
}


class _CollectingSink(Sink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def write(self, event: Event) -> None:
        self.events.append(event)

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


class _Fetcher:
    def __init__(self, document: dict[str, Any] | None) -> None:
        self.document = None if document is None else parse_document(json.dumps(document).encode())
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> _CollectingSink:
    collecting = _CollectingSink()
    monkeypatch.setattr("baton.client.HttpSink", lambda *_, **__: collecting)
    return collecting


def _use(monkeypatch: pytest.MonkeyPatch, document: dict[str, Any] | None) -> _Fetcher:
    fetcher = _Fetcher(document)
    monkeypatch.setattr(
        "baton.client.console_settings",
        lambda _dsn, _tenant, vendor_id, _refresh: CaptureSettings(
            vendor_id=vendor_id,
            fetcher=fetcher,  # type: ignore[arg-type]
        ),
    )
    return fetcher


ENTERPRISE = Principal(
    principal_id="u1", organization=Organization(organization_id="acme", labels=["enterprise"])
)


def _run(make: type[Client] | type[AsyncClient], principal: Principal | None) -> None:
    if make is Client:
        client = Client(dsn=DSN, settings="console")
        with client.session(principal=principal):
            with client.trace(tool_name="fetch") as trace:
                trace.observed({"note": "private"})
        client.close()
        return

    async def go() -> None:
        aclient = AsyncClient(dsn=DSN, settings="console")
        with aclient.session(principal=principal):
            async with aclient.trace(tool_name="fetch") as trace:
                trace.observed({"note": "private"})
        await aclient.aclose()

    asyncio.run(go())


def _end(sink: _CollectingSink) -> dict[str, Any]:
    (end,) = [e.model_dump(mode="json") for e in sink.events if e.event_type == "tool_call_end"]
    return end


CLIENTS = pytest.mark.parametrize("make", [Client, AsyncClient])


@CLIENTS
def test_a_caller_in_an_audience_with_results_off_sends_no_result(
    make: Any, sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, DOCUMENT)
    _run(make, ENTERPRISE)
    end = _end(sink)
    assert "private" not in json.dumps(end)
    assert end["payload"]["result_capture"] == "off"
    assert end["capture"] == {"settings_version": 4, "audience": "enterprise"}


@CLIENTS
def test_a_caller_in_another_audience_keeps_the_code_value(
    make: Any, sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, DOCUMENT)
    _run(make, Principal(principal_id="u2"))
    end = _end(sink)
    assert "private" in json.dumps(end)
    assert end["capture"] == {"settings_version": 4, "audience": "everyone"}


@CLIENTS
def test_before_the_first_read_the_code_values_apply(
    make: Any, sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, None)
    _run(make, ENTERPRISE)
    end = _end(sink)
    assert "private" in json.dumps(end)
    assert end.get("capture") is None


@CLIENTS
def test_the_settings_reader_starts_with_the_client_and_stops_at_close(
    make: Any, sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _use(monkeypatch, DOCUMENT)
    _run(make, None)
    assert fetcher.started and fetcher.stopped


EVERYONE_OFF = {
    "version": 5,
    "audiences": [{"id": "everyone", "match": {}, "capture": {"results": "off"}}],
}


def test_a_call_outside_a_block_runs_under_the_settings(
    sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, EVERYONE_OFF)
    client = Client(dsn=DSN, settings="console")
    with client.trace(tool_name="fetch") as trace:
        trace.observed({"note": "private"})
    client.annotate(what_happened="slow")
    client.close()
    end = _end(sink)
    assert "private" not in json.dumps(end)
    assert {json.dumps(e.model_dump(mode="json")["capture"]) for e in sink.events} == {
        json.dumps({"settings_version": 5, "audience": "everyone"})
    }


def test_a_block_open_across_the_first_read_picks_the_settings_up(
    sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _use(monkeypatch, None)
    client = Client(dsn=DSN, settings="console")
    with client.session(principal=ENTERPRISE):
        fetcher.document = parse_document(json.dumps(DOCUMENT).encode())
        with client.trace(tool_name="fetch") as trace:
            trace.observed({"note": "private"})
    client.close()
    assert _end(sink)["payload"]["result_capture"] == "off"


def test_the_block_still_shows_who_it_is_for(
    sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, None)
    client = Client(dsn=DSN, settings="console")
    with client.session(principal=ENTERPRISE) as block:
        assert block.principal is not None and block.principal.id == "u1"
        assert block.organization is not None
        assert block.labels == ["enterprise"]
    client.close()


def test_local_settings_never_read_the_console(
    sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: Any) -> None:
        raise AssertionError("read the Console")

    monkeypatch.setattr("baton.client.console_settings", refuse)
    Client(dsn=DSN).close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"settings": "remote"}, "must be one of"),
        ({"settings": "console", "settings_refresh_s": 1}, "at least 15"),
    ],
)
def test_bad_settings_are_refused_at_construction(
    sink: _CollectingSink, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Client(dsn=DSN, **kwargs)


def test_console_settings_need_a_dsn() -> None:
    with pytest.raises(ValueError, match="needs a dsn"):
        Client(vendor_id="v1", sink=_CollectingSink(), settings="console")


def test_the_off_switch_never_raises_over_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BATON_DISABLED", "1")
    Client(settings="console").close()


@CLIENTS
def test_a_client_dropped_without_close_stops_reading_the_console(
    make: Any, sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _use(monkeypatch, None)
    make(dsn=DSN, settings="console")
    gc.collect()
    assert fetcher.stopped
