"""The MCP install path against ``baton-spec``'s fake collector, in every
scenario of ``collector/intent.json``; what each checks is the file's ``about``."""

from __future__ import annotations

import json
from typing import Any

import pytest

from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.sinks import HttpSink
from tests._mcp_session import connected_session
from tests.functional.conftest import SPEC_ROOT
from tests.test_broken_collector import _received

_FILE = SPEC_ROOT / "collector" / "intent.json"
_SPEC: dict[str, Any] = json.loads(_FILE.read_text()) if _FILE.exists() else {"scenarios": []}
_NOT_RUN: dict[str, str] = {}
_MEMBERS = ("call_intent", "call_expected", "call_workflow", "intent_source")


@pytest.mark.skipif(not _FILE.exists(), reason="baton-spec submodule not checked out")
@pytest.mark.parametrize(
    ("scenario", "collector"),
    [
        pytest.param(
            s,
            {"mode": "ok"},
            id=s["name"],
            marks=[pytest.mark.skip(reason=_NOT_RUN[s["name"]])] if s["name"] in _NOT_RUN else [],
        )
        for s in _SPEC["scenarios"]
    ],
    indirect=["collector"],
)
async def test_user_goal_is_injected_and_read(scenario: dict[str, Any], collector: int) -> None:
    mcp = FastMCP("intent")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="v",
            vendor_display_name="V",
            sink=HttpSink(url=f"http://127.0.0.1:{collector}", api_key="k"),
        ),
    )
    async with connected_session(mcp) as session:
        # By alias: mcp 2.x renamed the attributes, not the wire names.
        [tool] = [t.model_dump(by_alias=True) for t in (await session.list_tools()).tools]
        assert tool["inputSchema"]["properties"]["user_goal"]["type"] == "string"
        assert "user_goal" in tool["inputSchema"]["required"]
        result = (await session.call_tool("echo", scenario["arguments"])).model_dump(by_alias=True)
        assert not result["isError"]
        assert result["content"][0]["text"] == scenario["arguments"]["text"]
    await handle.aclose()

    [start] = [
        e for e in _received(collector, "ok")["events"] if e["event_type"] == "tool_call_start"
    ]
    expect = scenario["expect"]
    assert start["payload"]["params"] == expect["params"]
    for member in _MEMBERS:
        assert start["payload"].get(member) == expect.get(member), member
