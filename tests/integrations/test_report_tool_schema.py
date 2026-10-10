"""``tool_name`` is advertised as required on a reports-only annotation tool,
and a report that omits it is still taken (SPEC §5.1.1)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client
from fastmcp import FastMCP as StandaloneServer

from baton.integrations import official, standalone
from baton.integrations._llm_text import build_refusal_text
from baton.integrations.official._compat import MCPServerClass as OfficialServer
from baton.sinks import FileSink


def _config(module: Any, path: str, proactive_mode: str) -> Any:
    return module.VendorConfig(
        vendor_id="v",
        vendor_display_name="V",
        consent_token="ct_test",
        sink=FileSink(path),
        proactive_mode=proactive_mode,
    )


async def _official(
    path: str, proactive_mode: str, report: dict[str, Any]
) -> tuple[list[str], str]:
    mcp = OfficialServer("srv")
    handle = official.install_baton(mcp, _config(official, path, proactive_mode))
    try:
        tool = next(t for t in await mcp.list_tools() if t.name == handle.annotation_tool_name)
        answer = await mcp.call_tool(handle.annotation_tool_name, report)
        return list(tool.model_dump(by_alias=True)["inputSchema"].get("required", [])), str(answer)
    finally:
        await handle.aclose()


async def _standalone(
    path: str, proactive_mode: str, report: dict[str, Any]
) -> tuple[list[str], str]:
    mcp = StandaloneServer("srv")
    handle = standalone.install_baton(mcp, _config(standalone, path, proactive_mode))
    try:
        async with Client(mcp) as client:
            tool = next(
                t for t in await client.list_tools() if t.name == handle.annotation_tool_name
            )
            answer = await client.call_tool(handle.annotation_tool_name, report)
        return (
            list(tool.model_dump(by_alias=True)["inputSchema"].get("required", [])),
            str(answer.content),
        )
    finally:
        await handle.aclose()


REPORT = {"user_goal": "remove the milk", "what_happened": "nothing removes an item"}


@pytest.mark.parametrize("adapter", [_official, _standalone])
async def test_a_reports_only_tool_lists_tool_name_as_required(adapter: Any, tmp_path: Any) -> None:
    path = str(tmp_path / "events.jsonl")

    required, _ = await adapter(path, "off", REPORT)

    assert "tool_name" in required
    assert "what_happened" not in required
    reports = [
        e["payload"]
        for e in map(json.loads, open(path).read().splitlines())
        if e["event_type"] == "annotation"
    ]
    assert [r["what_happened"] for r in reports] == [REPORT["what_happened"]]
    assert reports[0]["tool_name"] is None


@pytest.mark.parametrize("adapter", [_official, _standalone])
async def test_a_tool_that_also_takes_notes_leaves_tool_name_optional(
    adapter: Any, tmp_path: Any
) -> None:
    required, _ = await adapter(str(tmp_path / "events.jsonl"), "on", REPORT)

    assert "tool_name" not in required


@pytest.mark.parametrize("adapter", [_official, _standalone])
@pytest.mark.parametrize("account", [{}, {"what_happened": "   "}])
async def test_a_reports_only_tool_refuses_a_call_with_no_account(
    adapter: Any, account: dict[str, str], tmp_path: Any
) -> None:
    path = tmp_path / "events.jsonl"

    _, answer = await adapter(str(path), "off", {"user_goal": "about to look", **account})

    refusal = build_refusal_text(annotation_tool_name="v_annotate")
    assert refusal in answer
    events = map(json.loads, path.read_text().splitlines()) if path.exists() else []
    assert [e for e in events if e["event_type"] == "annotation"] == []
