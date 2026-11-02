"""The MCP install path against ``baton-spec``'s fake collector, in every
scenario of ``collector/intent.json``; what each checks is the file's ``about``."""

from __future__ import annotations

from typing import Any

from tests._fake_collector import each_scenario, http_sink, install_echo, received, scenario_file
from tests._mcp_session import connected_session

_SPEC = scenario_file("intent.json")
_MEMBERS = ("call_intent", "call_expected", "call_workflow", "intent_source")


@each_scenario(_SPEC, mode="ok")
async def test_user_goal_is_injected_and_read(scenario: dict[str, Any], collector: int) -> None:
    mcp, handle = install_echo(http_sink(collector))
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
        e for e in (await received(collector))["events"] if e["event_type"] == "tool_call_start"
    ]
    expect = scenario["expect"]
    assert start["payload"]["params"] == expect["params"]
    for member in _MEMBERS:
        assert start["payload"].get(member) == expect.get(member), member
