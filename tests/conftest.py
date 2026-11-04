"""Suite-wide fixtures.

**No ambient ``BATON_*`` environment variable reaches a test that did not ask
for one.** The prefix is ours, so the fixture scrubs the whole namespace rather
than a list — a variable added to the SDK later is covered without anybody
remembering to edit this file, which is how the list got out of date the first
time.

Two reasons it exists, one per variable that has already caused trouble:

- ``BATON_DISABLED`` turns capture off, and a developer who has it exported
  would otherwise watch a large part of this suite fail with "no events
  arrived" and no visible explanation — the line saying why is at INFO, where
  pytest shows nothing by default. Measured: with this fixture removed and
  ``BATON_DISABLED=1`` exported, most of the suite fails — around two hundred
  tests, though the exact figure moves with which tests a machine skips, so do
  not treat it as a fixed number.
- ``BATON_DSN`` is the WIDER exposure, and it was not scrubbed until the DSN
  parser's leaks were fixed. An ambient DSN supplies the vendor id, the tenant
  id AND the sink — so a developer with one exported for a real server had this
  suite building ``HttpSink``s at a live collector and POSTing fixture events
  into a real workspace. A failing test is a nuisance; that is test data in
  production.

⚠ **Scrubbing only those two was not enough, and the docstring said otherwise.**
Found by review of the commit that added ``BATON_DSN``: five more were still
getting through. Measured — with ``BATON_VENDOR_ID``, ``BATON_TENANT_ID``,
``BATON_CONSENT_TOKEN`` and ``BATON_INTENT_PARAM`` exported, **five tests
failed**, two of them in the DSN lane's own parity file, and
``test_vendor_id_required`` did not raise at all. The claim in this docstring
is what made the gap worth finding: a guarantee stated in bold is one somebody
will rely on.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from pytest_httpserver import HTTPServer

from baton._state import PROCESS_DROPS
from baton.sinks import _LIVE_SENDERS, _QueueSender
from tests._fake_collector import COLLECTOR_DIR
from tests._http_fakes import RELEASE


@pytest.fixture(autouse=True)
def _fresh_drop_counts() -> Iterator[None]:
    """``PROCESS_DROPS`` is one per process, so each test starts from zero."""
    PROCESS_DROPS._reset()
    yield


@pytest.fixture(autouse=True)
def _release_held_requests() -> Iterator[None]:
    RELEASE.clear()
    yield
    RELEASE.set()


@pytest.fixture(autouse=True)
def _stop_every_sender() -> Iterator[None]:
    """A test that ends its sink with ``_closed = True`` leaves the sending
    thread alive, and it would send into the next test's server and log."""
    yield
    for sender in list(_LIVE_SENDERS):
        sender.stop()


@pytest.fixture(autouse=True)
def _no_ambient_baton_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse and suite-wide. Tests that WANT a variable set it themselves
    with ``monkeypatch.setenv`` afterwards, which wins — this runs first."""
    for name in [name for name in os.environ if name.startswith("BATON_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def make_httpserver(
    httpserver_listen_address: tuple[str | None, int | None],
) -> Iterator[HTTPServer]:
    # Threaded: the default server takes one connection at a time, so a test
    # that leaves a connection open stalls every later test's events:
    # `test_event_server.py`.
    host, port = httpserver_listen_address
    server = HTTPServer(
        host=host or HTTPServer.DEFAULT_LISTEN_HOST,
        port=port or HTTPServer.DEFAULT_LISTEN_PORT,
        threaded=True,
    )
    server.start()
    yield server
    server.clear()
    if server.is_running():
        server.stop()


@pytest.fixture
def collector(request: pytest.FixtureRequest) -> Iterator[int]:
    """The port of a collector in the scenario's mode, or 1, where nothing
    listens, for "down"."""
    scenario: dict[str, Any] = request.param
    if scenario["mode"] == "down":
        yield 1
        return
    process = subprocess.Popen(
        [
            sys.executable,
            str(COLLECTOR_DIR / "fake_collector.py"),
            "--mode",
            scenario["mode"],
            "--delay-ms",
            str(scenario.get("delay_ms", 0)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        yield int(process.stdout.readline())
    finally:
        process.kill()
        process.wait()


@pytest.fixture(autouse=True)
def _held_queue_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HOLD_SINK_QUEUE=1`` holds every queued write until a flush, a close,
    or ``queues_written()`` lets it through, so a test that reads a sink
    without waiting sees nothing, however long its teardown takes. Under it,
    ``test_queued_sink``'s two drain-died tests fail: they need a write to run
    before any flush."""
    if os.environ.get("HOLD_SINK_QUEUE") != "1":
        return
    write = _QueueSender._write

    async def held_write(self: _QueueSender, event: Any) -> None:
        while not getattr(self, "released", False):
            await asyncio.sleep(0.001)
        await write(self, event)

    def releasing(wait: Any) -> Any:
        async def run(self: _QueueSender, *args: Any) -> None:
            self.released = True  # type: ignore[attr-defined]
            try:
                await wait(self, *args)
            finally:
                self.released = False  # type: ignore[attr-defined]

        return run

    monkeypatch.setattr(_QueueSender, "_write", held_write)
    monkeypatch.setattr(_QueueSender, "flush_inner", releasing(_QueueSender.flush_inner))
    monkeypatch.setattr(_QueueSender, "close_inner", releasing(_QueueSender.close_inner))
    monkeypatch.setattr(_QueueSender, "_retire", releasing(_QueueSender._retire))
