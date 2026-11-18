"""The test run's task id: ``BATON_TASK`` and the ``Baton-Task`` header, sent
as ``capture.task`` beside ``capture.candidate`` (SPEC §11.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from baton import Client as LibraryClient
from baton._capture_settings import Caller, CaptureSettings, header_task, task_id
from baton.events import CaptureObserved
from baton.integrations import _config as config_module
from baton.integrations import standalone
from baton.sinks import FileSink
from tests._candidate_helpers import (
    CHANGE,
    CHANGED,
    Collector,
    held,
    started,
)

DSN = "https://baton_pk_x@h.example.com/ten_" + "0" * 32 + "/srv"
TASK = "task_1"


def _capture(settings: CaptureSettings, caller: Caller) -> CaptureObserved:
    call = settings.for_caller(caller)
    assert call is not None
    return call.capture


def _settled(settings: CaptureSettings, caller: Caller) -> CaptureObserved:
    settings.for_caller(caller)
    held(settings, CHANGE)
    return _capture(settings, caller)


class TestVariable:
    def test_the_task_rides_with_the_variables_change(self) -> None:
        settings = started(Collector({CHANGE: CHANGED}), CHANGE, TASK)
        capture = _capture(settings, Caller())
        assert (capture.candidate, capture.task) == (CHANGE, TASK)
        assert capture.model_dump(exclude_unset=True)["task"] == TASK

    def test_no_task_without_a_change(self) -> None:
        settings = started(Collector({}), None, TASK)
        capture = _capture(settings, Caller())
        assert capture.task is None
        assert "task" not in capture.model_dump(exclude_unset=True)

    def test_no_task_when_the_change_did_not_load(self) -> None:
        settings = started(Collector({CHANGE: 404}), CHANGE, TASK)
        assert _capture(settings, Caller()).task is None

    def test_console_settings_read_the_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BATON_TASK", TASK)
        config = config_module.resolve_config(standalone.VendorConfig(dsn=DSN, settings="console"))
        settings = config_module.build_capture_settings(config, "ten_1")
        assert settings is not None and settings._task == TASK

    def test_a_variable_that_cannot_be_a_task_id_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BATON_TASK", "no spaces")
        config = config_module.resolve_config(standalone.VendorConfig(dsn=DSN, settings="console"))
        settings = config_module.build_capture_settings(config, "ten_1")
        assert settings is not None and settings._task is None


class TestHeader:
    def test_the_headers_task_rides_with_the_headers_change(self) -> None:
        settings = started(Collector({CHANGE: CHANGED}))
        capture = _settled(settings, Caller(candidate=CHANGE, task=TASK))
        assert (capture.candidate, capture.task) == (CHANGE, TASK)

    def test_a_header_change_does_not_take_the_variables_task(self) -> None:
        settings = started(Collector({CHANGE: CHANGED}), None, "task_env")
        capture = _settled(settings, Caller(candidate=CHANGE))
        assert (capture.candidate, capture.task) == (CHANGE, None)

    def test_a_header_task_without_a_change_is_not_sent(self) -> None:
        settings = started(Collector({}))
        assert _capture(settings, Caller(task=TASK)).task is None

    def test_the_header_is_read_from_each_adapters_mapping(self) -> None:
        assert header_task({"baton-task": TASK}) == TASK
        assert header_task({}) is None
        assert header_task(None) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("run:7.task_1-a", "run:7.task_1-a"),
        ("  task_1 ", "task_1"),
        ("x" * 128, "x" * 128),
        ("x" * 129, None),
        ("", None),
        ("a/b", None),
        ("a b", None),
        (7, None),
    ],
)
def test_only_an_id_of_the_specs_shape_is_kept(value: object, expected: str | None) -> None:
    assert task_id(value) == expected


def test_the_library_path_takes_the_header(tmp_path: Path) -> None:
    from baton._library_session import session_for_call

    settings = started(Collector({CHANGE: CHANGED}))
    client = LibraryClient(sink=FileSink(str(tmp_path / "e.jsonl")), vendor_id="srv")
    headers = {"Baton-Candidate": CHANGE, "Baton-Task": TASK}
    try:
        with client.session(headers=headers):
            session_for_call(client, settings)
        held(settings, CHANGE)
        with client.session(headers=headers):
            caller = session_for_call(client, settings).caller
        assert caller.settings is not None
        assert (caller.settings.capture.candidate, caller.settings.capture.task) == (CHANGE, TASK)
    finally:
        client.close()
