"""The off switch — ``BATON_DISABLED`` and ``DO_NOT_TRACK``.

Two promises, and each one is a way the switch could be worse than not having
it at all:

1. **Nothing on stdout.** A stdio MCP server speaks JSON-RPC there, so a
   courteous "Baton is disabled" line breaks the server in exactly the
   deployment this switch exists for. ``T20`` bans ``print()`` in ``src/`` but
   says nothing about ``sys.stdout.write`` or a logger someone points at
   stdout, so the promise needs its own pin.
2. **It cannot break a boot.** Off means install nothing AND never throw. A
   README paragraph promising "set this and Baton stops" is worse than no
   paragraph if setting it can abort the vendor's server — the user believes
   they opted out, and the thing they opted out of took the process with it.

⚠ **The consequence, pinned deliberately rather than left to be discovered:** a
malformed ``install_baton`` call cannot fail while the switch is on. A vendor
whose CI exports ``DO_NOT_TRACK`` globally learns about it the first time
capture is enabled. That is the cost of promise 2, and the tests below assert
it on purpose so nobody "fixes" it into a raise.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from baton._optout import capture_disabled

SWITCH = "BATON_DISABLED"

# Reversed 2026-09-10 — pinned below so it cannot creep back in unnoticed.
REVERSED = "DO_NOT_TRACK"


class TestWhichValuesCount:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "anything"])
    def test_a_value_that_is_not_an_explicit_off_disables(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Permissive toward the opt-out on purpose. The two failure directions
        are not symmetric: honouring an unintended opt-out costs telemetry,
        ignoring an intended one collects data from someone who asked us not
        to."""
        monkeypatch.setenv(SWITCH, value)
        assert capture_disabled() == SWITCH

    @pytest.mark.parametrize("value", ["", "0", "false", "FALSE", "no", "off", "  0  "])
    def test_an_explicit_off_does_not_disable(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """``DO_NOT_TRACK=0`` is someone saying tracking is FINE. Reading it as
        "the variable is present, therefore opted out" would ignore the only
        thing they actually said."""
        monkeypatch.setenv(SWITCH, value)
        assert capture_disabled() is None

    def test_unset_is_not_disabled(self) -> None:
        assert capture_disabled() is None

    @pytest.mark.parametrize("value", ["1", "true", "yes"])
    def test_DO_NOT_TRACK_does_NOT_disable_and_that_is_deliberate(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """It was implemented, then reversed 2026-09-10 — measured, not argued.

        An MCP client hands the server it spawns a fixed allowlist (``HOME``,
        ``LOGNAME``, ``PATH``, ``SHELL``, ``TERM``, ``USER``), and
        ``DO_NOT_TRACK`` is not on it. So a global export never reaches a stdio
        server, and a user who edits their client config's ``env`` block to add
        it could have typed ``BATON_DISABLED=1`` there instead — the
        convenience the convention was worth buying does not exist in this
        deployment model. What it did cost was a contributor with the variable
        exported getting a silently disabled SDK and a red suite.

        This test is the record. If it starts failing, someone re-added the
        variable; read ``baton._optout``'s module docstring before deciding
        that is right."""
        monkeypatch.setenv("DO_NOT_TRACK", value)
        assert capture_disabled() is None


class TestTheInstallDoorInstallsNothing:
    async def test_the_server_is_left_exactly_as_it_was(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted on the SERVER, not on the handle. A handle that reports
        itself disabled while the middleware is attached and the annotation
        tool is on the surface is the failure this test exists to catch."""
        from fastmcp import Client, FastMCP

        from baton.install import install_baton

        mcp: Any = FastMCP("optout")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            before = sorted(t.name for t in await client.list_tools())
        instructions_before = mcp.instructions

        monkeypatch.setenv(SWITCH, "1")
        install_baton(mcp, dsn="https://baton_pk_x@h.example.com/ten_" + "0" * 32 + "/srv")

        async with Client(mcp) as client:
            after = sorted(t.name for t in await client.list_tools())
            # And it still WORKS — the point is a server that behaves as if
            # install_baton were not in the file.
            result = await client.call_tool("echo", {"text": "hi"})
        assert after == before, "a tool appeared or vanished under the off switch"
        assert mcp.instructions == instructions_before, "the instructions were rewritten"
        assert result is not None

    async def test_a_driven_tool_call_emits_nothing_through_a_WORKING_sink(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The install door's twin of the library-door test above.

        A real ``FileSink`` the vendor supplied, a real client calling a real
        tool — and an empty file. The second half drives the identical rig with
        the switch off, so a build where nothing was ever captured for an
        unrelated reason cannot pass the first half.
        """
        from fastmcp import Client, FastMCP

        from baton.install import install_baton
        from baton.integrations._config import VendorConfig
        from baton.sinks import FileSink

        events = tmp_path / "events.jsonl"

        async def _run() -> None:
            mcp: Any = FastMCP("optout")

            @mcp.tool()
            def echo(text: str) -> str:
                return text

            handle = install_baton(
                mcp,
                VendorConfig(vendor_id="v", vendor_display_name="V", sink=FileSink(str(events))),
            )
            try:
                async with Client(mcp) as client:
                    await client.call_tool("echo", {"text": "hi"})
            finally:
                await handle.aclose()

        monkeypatch.setenv(SWITCH, "1")
        await _run()
        assert not events.exists() or events.read_text() == ""

        monkeypatch.delenv(SWITCH)
        await _run()
        assert events.read_text().strip(), "the same rig captures nothing with the switch OFF"

    def test_a_dsn_builds_no_sink_and_therefore_no_http_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The switch has to be read BEFORE the config is built, or a DSN
        constructs an ``HttpSink`` — and its httpx client — for capture that is
        never going to happen."""
        from fastmcp import FastMCP

        from baton._optout import DisabledSink
        from baton.install import install_baton

        monkeypatch.setenv(SWITCH, "1")
        handle = install_baton(
            FastMCP("optout"),
            dsn="https://baton_pk_x@h.example.com/ten_" + "0" * 32 + "/srv",
        )
        assert isinstance(handle.sink, DisabledSink)


class TestEachAdapterCarriesItsOwnGuard:
    """Not just the router — and a mutation is what said so.

    Every other test here calls ``baton.install_baton``, which checks the
    switch before it routes. So removing the standalone adapter's OWN check
    reddened nothing, and the adapters are the shipped public entry points:
    ``from baton.integrations.standalone import install_baton`` is what a
    vendor's file says, and what the Console's recipe writes. A guard only the
    router holds is a guard half the callers never reach.
    """

    async def test_the_standalone_adapter_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastmcp import Client, FastMCP

        from baton._optout import DisabledSink
        from baton.integrations.standalone import install_baton

        mcp: Any = FastMCP("optout-direct")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        monkeypatch.setenv(SWITCH, "1")
        handle = install_baton(mcp, dsn="https://baton_pk_x@h/ten_" + "0" * 32 + "/s")

        assert isinstance(handle.sink, DisabledSink)
        async with Client(mcp) as client:
            assert sorted(t.name for t in await client.list_tools()) == ["echo"]

    async def test_the_official_adapter_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from baton._optout import DisabledSink
        from baton.integrations.official import install_baton
        from baton.integrations.official._compat import MCPServerClass as FastMCP
        from tests._mcp_session import connected_session

        mcp = FastMCP("optout-direct")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        monkeypatch.setenv(SWITCH, "1")
        handle = install_baton(mcp, dsn="https://baton_pk_x@h/ten_" + "0" * 32 + "/s")

        assert isinstance(handle.sink, DisabledSink)
        async with connected_session(mcp) as client:
            listed = await client.list_tools()
            assert sorted(t.name for t in listed.tools) == ["echo"]

    def test_each_adapter_refuses_to_throw_on_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both adapters' shape guards raise when the switch is off; neither
        may raise while it is on."""
        from baton.integrations.official import install_baton as official_install
        from baton.integrations.standalone import install_baton as standalone_install

        monkeypatch.setenv(SWITCH, "1")
        assert standalone_install(object()) is not None  # type: ignore[arg-type]
        assert official_install(object()) is not None  # type: ignore[arg-type]


class TestItCannotBreakABoot:
    """Every one of these raises when the switch is OFF. That is the point."""

    def test_an_object_that_is_no_kind_of_server_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from baton.install import install_baton

        monkeypatch.setenv(SWITCH, "1")
        assert install_baton(object()) is not None

    def test_a_config_missing_its_vendor_id_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastmcp import FastMCP

        from baton.install import install_baton
        from baton.integrations._config import VendorConfig

        monkeypatch.setenv(SWITCH, "1")
        assert install_baton(FastMCP("x"), VendorConfig()) is not None

    def test_an_unparseable_dsn_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastmcp import FastMCP

        from baton.install import install_baton

        monkeypatch.setenv(SWITCH, "1")
        assert install_baton(FastMCP("x"), dsn="not-a-dsn-at-all") is not None

    def test_nothing_at_all_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without the switch this is the "needs either a VendorConfig or a
        dsn" refusal."""
        from fastmcp import FastMCP

        from baton.install import install_baton

        monkeypatch.setenv(SWITCH, "1")
        assert install_baton(FastMCP("x")) is not None

    def test_the_handle_survives_the_vendors_finally_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vendor code calls ``flush()``/``aclose()`` in a ``finally``. A
        handle that explodes there is the switch breaking the server through
        the back door."""
        from fastmcp import FastMCP

        from baton.install import install_baton

        monkeypatch.setenv(SWITCH, "1")
        handle = install_baton(FastMCP("x"), dsn="https://baton_pk_x@h/ten_" + "0" * 32 + "/s")

        import asyncio

        async def _drive() -> dict[str, str | None]:
            await handle.flush()
            ticket = await handle.escalate()
            await handle.aclose()
            return ticket

        # escalate() takes the existing no-Console-URL path rather than needing
        # a branch of its own.
        assert asyncio.run(_drive())["ticket_id"] == "queued"


class TestNothingReachesStdout:
    """The trap that breaks the server rather than merely annoying someone."""

    def test_the_install_path_writes_nothing_to_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from fastmcp import FastMCP

        from baton.install import install_baton

        monkeypatch.setenv(SWITCH, "1")
        install_baton(FastMCP("x"), dsn="https://baton_pk_x@h/ten_" + "0" * 32 + "/s")
        assert capsys.readouterr().out == ""

    def test_the_library_path_writes_nothing_to_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from baton import Client

        monkeypatch.setenv(SWITCH, "1")
        client = Client()
        try:
            with client.trace(tool_name="t") as trace:
                trace.observed({"ok": True})
        finally:
            client.close()
        assert capsys.readouterr().out == ""

    def test_it_says_so_on_the_LOGGER_naming_which_variable(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silence would leave "why are no events arriving" answerable only by
        reading source. The usual answer is a DO_NOT_TRACK somebody exported
        months ago for something else."""
        from fastmcp import FastMCP

        from baton.install import install_baton

        monkeypatch.setenv(SWITCH, "1")
        with caplog.at_level(logging.INFO, logger="baton._optout"):
            install_baton(FastMCP("x"), dsn="https://baton_pk_x@h/ten_" + "0" * 32 + "/s")
        assert SWITCH in caplog.text


class TestTheLibraryDoor:
    def test_a_disabled_sync_client_starts_no_background_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "No wrap, no buffer, no queue" — and the sync client is the one path
        with no wrap to skip, so the daemon thread IS the promise here.
        ``_SyncBridge.__init__`` starts it unconditionally."""
        monkeypatch.setenv(SWITCH, "1")
        from baton import Client

        before = {t.name for t in threading.enumerate()}
        client = Client()
        try:
            assert client._bridge is None
            new_threads = {t.name for t in threading.enumerate()} - before
            assert not any(name.startswith("baton-") for name in new_threads), new_threads
        finally:
            client.close()

    def test_an_enabled_sync_client_still_starts_one(self) -> None:
        """The mutation guard: without it, the test above passes on a build
        where the bridge never starts for anybody."""
        from baton import Client
        from baton.sinks import StdoutSink

        client = Client(vendor_id="v", sink=StdoutSink())
        try:
            assert client._bridge is not None
        finally:
            client.close()

    async def test_a_disabled_async_client_ignores_a_WORKING_sink(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Handed a real ``FileSink``, it must still write nothing.

        Asserting on an unconfigured client would prove only that nothing was
        set up. The switch has to beat a sink the vendor explicitly supplied
        and that demonstrably works when the switch is off — which the second
        half of this test checks, so a build where the FileSink is simply
        broken cannot pass the first half.
        """
        events = tmp_path / "events.jsonl"
        monkeypatch.setenv(SWITCH, "1")

        from baton import AsyncClient
        from baton.sinks import FileSink

        client = AsyncClient(vendor_id="v", sink=FileSink(str(events)))
        try:
            async with client.trace(tool_name="t") as trace:
                trace.observed({"ok": True})
        finally:
            await client.aclose()
        assert not events.exists() or events.read_text() == ""

        monkeypatch.delenv(SWITCH)
        enabled = AsyncClient(vendor_id="v", sink=FileSink(str(events)))
        try:
            async with enabled.trace(tool_name="t") as trace:
                trace.observed({"ok": True})
        finally:
            await enabled.aclose()
        assert events.read_text().strip(), "the same sink writes nothing with the switch OFF"

    def test_trace_and_annotate_still_work_while_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The vendor's code holds these objects and calls methods on them.
        Emitting nothing must not mean returning nothing."""
        from baton import Client, SignalType

        monkeypatch.setenv(SWITCH, "1")
        client = Client()
        try:
            with client.trace(tool_name="work", intent="do a thing", params={"a": 1}) as trace:
                trace.observed({"result": "ok"})
            client.annotate(signal_type=SignalType.DEAD_END, suggested_improvement="x")
        finally:
            client.close()


class TestASinkYouPassedIsNotDroppedOnTheFloor:
    """The client took ownership of that object when it was handed over, and
    ``close()`` is what releases it. Switching capture off does not undo that
    — a resource the vendor expected us to close would simply never be closed.

    ⚠ **Keeping it is also how the switch nearly broke.** While a disabled
    client was handed a no-op sink, ``AsyncClient._emit`` needed no guard and
    had none: nothing could reach a real destination. The moment the caller's
    sink was kept, every disabled async client emitted for real. The tests
    below hand a disabled client a WORKING sink for exactly that reason —
    against a no-op sink they would pass on a build with the switch removed.
    """

    def test_a_disabled_sync_client_keeps_and_closes_the_sink_it_was_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from baton import Client
        from baton.sinks import FileSink

        events = tmp_path / "events.jsonl"
        sink = FileSink(str(events))
        monkeypatch.setenv(SWITCH, "1")

        client = Client(vendor_id="v", sink=sink)
        try:
            assert client._sink is sink, "the caller's sink was swapped out and dropped"
            with client.trace(tool_name="t") as trace:
                trace.observed({"ok": True})
        finally:
            client.close()

        assert not events.exists() or events.read_text() == "", "a disabled client emitted"
        # Closed even though there is no bridge thread to close it on.
        assert sink._closed is True

    async def test_a_disabled_async_client_keeps_and_closes_the_sink_it_was_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from baton import AsyncClient
        from baton.sinks import FileSink

        sink = FileSink(str(tmp_path / "events.jsonl"))
        monkeypatch.setenv(SWITCH, "1")

        client = AsyncClient(vendor_id="v", sink=sink)
        try:
            assert client._sink is sink
        finally:
            await client.aclose()
        assert sink._closed is True

    def test_the_install_handle_keeps_the_sink_from_the_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from fastmcp import FastMCP

        from baton.install import install_baton
        from baton.integrations._config import VendorConfig
        from baton.sinks import FileSink

        sink = FileSink(str(tmp_path / "events.jsonl"))
        monkeypatch.setenv(SWITCH, "1")
        handle = install_baton(
            FastMCP("x"), VendorConfig(vendor_id="v", vendor_display_name="V", sink=sink)
        )
        assert handle.sink is sink


class TestEscalateNamesTheRightCause:
    """``escalate()`` reused the dev-mode message under the off switch, which
    said "sink has no Console URL … switch to HttpSink" — a confident,
    actionable, WRONG diagnosis, at WARNING where it is the only line a vendor
    sees, while the true cause sat at INFO where nothing shows it. A vendor
    with a perfectly good dsn would swap sinks, redeploy, and change nothing.
    """

    async def test_it_names_the_switch_not_the_sink(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from fastmcp import FastMCP

        from baton.install import install_baton

        monkeypatch.setenv(SWITCH, "1")
        handle = install_baton(FastMCP("x"), dsn="https://baton_pk_x@h/ten_" + "0" * 32 + "/s")

        with caplog.at_level(logging.WARNING, logger="baton"):
            ticket = await handle.escalate()

        assert ticket["ticket_id"] == "queued"
        assert SWITCH in caplog.text
        assert "Switch to HttpSink" not in caplog.text

    async def test_a_disabled_handle_holding_a_real_HttpSink_makes_no_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sharp edge of keeping the caller's sink: a disabled handle can
        now be holding a working ``HttpSink``. ``escalate()`` reads its url and
        api_key, so without the switch check it would make a REAL network call
        from a Baton that is supposed to be doing nothing."""
        from fastmcp import FastMCP

        from baton.install import install_baton
        from baton.integrations._config import VendorConfig
        from baton.sinks import HttpSink

        monkeypatch.setenv(SWITCH, "1")
        handle = install_baton(
            FastMCP("x"),
            VendorConfig(
                vendor_id="v",
                vendor_display_name="V",
                # A host that would fail loudly if anything dialled it.
                sink=HttpSink("https://console.invalid", api_key="k"),
            ),
        )
        assert handle._console_url is None
        assert (await handle.escalate())["ticket_id"] == "queued"
