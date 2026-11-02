"""A surface snapshot recorded after close is counted ``after_close`` like any
other event, with the same one warning, on both adapters (SPEC §11.2 rules 7, 8)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from baton._state import PROCESS_DROPS
from baton.sinks import FileSink
from tests._mcp_session import connected_session

_CALLS = 2
_LISTS = 2


async def _official(sink: FileSink) -> None:
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP

    mcp = FastMCP("after-close")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    handle = install_baton(mcp, VendorConfig(vendor_id="v", vendor_display_name="V", sink=sink))
    await handle.aclose()
    async with connected_session(mcp) as session:
        for _ in range(_LISTS):
            await session.list_tools()
        for _ in range(_CALLS):
            await session.call_tool("echo", {"text": "hi"})


async def _standalone(sink: FileSink) -> None:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton

    mcp = FastMCP("after-close")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    handle = install_baton(mcp, VendorConfig(vendor_id="v", vendor_display_name="V", sink=sink))
    await handle.aclose()
    async with Client(mcp) as client:
        for _ in range(_LISTS):
            await client.list_tools()
        for _ in range(_CALLS):
            await client.call_tool("echo", {"text": "hi"})


@pytest.mark.parametrize("drive", [_official, _standalone], ids=["official", "standalone"])
async def test_a_snapshot_after_close_is_counted_with_the_one_warning(
    drive: object, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="baton")
    await drive(FileSink(tmp_path / "events.jsonl"))  # type: ignore[operator]
    baton = [r for r in caplog.records if r.name == "baton" or r.name.startswith("baton.")]
    assert [r.exc_info for r in baton] == [None]
    # Each call's and each tools/list's start and end, and the snapshot once.
    assert PROCESS_DROPS.total() == {"after_close": 2 * _CALLS + 2 * _LISTS + 1}
