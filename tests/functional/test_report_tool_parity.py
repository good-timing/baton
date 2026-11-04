"""``report_tool`` is the annotation tool each MCP adapter registers (SPEC §5.6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from baton import ReportTool, report_tool
from tests._tools import tool_schema

pytestmark = pytest.mark.functional

DISPLAY_NAME = "Acme Data"


def _config(config_class: Any, events_path: Path) -> Any:
    from baton.sinks import FileSink

    return config_class(
        vendor_id="acme",
        vendor_display_name=DISPLAY_NAME,
        consent_token="ct_acme",
        sink=FileSink(str(events_path)),
        report_mode="on",
    )


def _without_titles(schema: Any) -> Any:
    """The schema as the library builds it: pydantic's titles and the
    standalone adapter's closed object are the host's, not the tool's."""
    if isinstance(schema, dict):
        return {
            k: _without_titles(v)
            for k, v in schema.items()
            if k != "title" and not (k == "additionalProperties" and v is False)
        }
    if isinstance(schema, list):
        return [_without_titles(v) for v in schema]
    return schema


async def _registered_official(events_path: Path) -> dict[str, Any]:
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass
    from tests._mcp_session import connected_session

    mcp = MCPServerClass(DISPLAY_NAME)
    handle = install_baton(mcp, _config(VendorConfig, events_path))
    try:
        async with connected_session(mcp) as client:
            (tool,) = (await client.list_tools()).tools
            refused = await client.call_tool(tool.name, {"user_goal": "1: x", "tool_name": "none"})
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool_schema(tool),
            "instructions": mcp.instructions,
            "refusal": json.loads(refused.content[0].text)["error"],
        }
    finally:
        await handle.aclose()


async def _registered_standalone(events_path: Path) -> dict[str, Any]:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton

    mcp: Any = FastMCP(DISPLAY_NAME)
    handle = install_baton(mcp, _config(VendorConfig, events_path))
    try:
        async with Client(mcp) as client:
            (tool,) = await client.list_tools()
            refused = await client.call_tool(tool.name, {"user_goal": "1: x", "tool_name": "none"})
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool_schema(tool),
            "instructions": mcp.instructions,
            "refusal": refused.structured_content["error"],
        }
    finally:
        await handle.aclose()


@pytest.mark.parametrize("registered", [_registered_official, _registered_standalone])
async def test_the_data_is_what_the_adapter_registers(registered: Any, tmp_path: Path) -> None:
    adapter = await registered(tmp_path / "events.jsonl")

    tool = _tool()

    assert tool.name == adapter["name"]
    assert tool.description == adapter["description"]
    assert tool.input_schema == _without_titles(adapter["input_schema"])
    assert tool.instructions == adapter["instructions"]
    assert tool.refusal == adapter["refusal"]


def _tool() -> ReportTool:
    return report_tool(vendor_display_name=DISPLAY_NAME)


def test_a_report_maps_to_annotate_and_drops_what_annotate_does_not_take() -> None:
    kwargs = _tool().annotate_kwargs(
        {
            "user_goal": "2: find the row; success is one row",
            "what_happened": "The search returned every row.",
            "tool_name": "search",
            "suggested_improvement": "Filter by id.",
            "context": {"likely_cause": "no filter"},
            "expected_result": "retired",
            "overall_task": "retired",
            "signal_type": "unknown",
        }
    )

    assert kwargs == {
        "intent": "2: find the row; success is one row",
        "what_happened": "The search returned every row.",
        "tool_name": "search",
        "suggested_improvement": "Filter by id.",
        "context": {"likely_cause": "no filter"},
    }


@pytest.mark.parametrize("tool_name", ["none", ""])
def test_no_tool_is_sent_as_the_agent_wrote_it(tool_name: str) -> None:
    kwargs = _tool().annotate_kwargs({"what_happened": "No tool exports.", "tool_name": tool_name})

    assert kwargs == {"what_happened": "No tool exports.", "tool_name": tool_name}


@pytest.mark.parametrize("what_happened", [None, "", "  \n", 7])
def test_no_account_is_no_report(what_happened: Any) -> None:
    """SPEC §5.6 rule 1: an intent-only annotation would be sent as a note."""
    arguments = {"user_goal": "1: x; success is y", "tool_name": "search"}
    if what_happened is not None:
        arguments["what_happened"] = what_happened

    assert _tool().annotate_kwargs(arguments) is None


def test_an_empty_value_is_absent_as_the_adapters_send_it() -> None:
    kwargs = _tool().annotate_kwargs(
        {"what_happened": "Broke.", "user_goal": "", "suggested_improvement": "", "context": {}}
    )

    assert kwargs == {"what_happened": "Broke."}


def test_a_value_of_the_wrong_type_is_dropped() -> None:
    kwargs = _tool().annotate_kwargs(
        {"what_happened": "Broke.", "tool_name": 3, "user_goal": ["x"], "context": "text"}
    )

    assert kwargs == {"what_happened": "Broke."}


def test_the_kwargs_are_what_annotate_takes() -> None:
    from baton import Client
    from baton.sinks import StdoutSink

    client = Client(vendor_id="acme", sink=StdoutSink())
    kwargs = _tool().annotate_kwargs(
        {
            "user_goal": "1: x; success is y",
            "what_happened": "Broke.",
            "tool_name": "search",
            "suggested_improvement": "Fix.",
            "context": {"a": 1},
        }
    )

    assert kwargs is not None
    client.annotate(**kwargs)


def test_a_name_is_validated_as_the_adapters_validate_it() -> None:
    assert report_tool(vendor_display_name=DISPLAY_NAME, name="acme_report").name == "acme_report"
    with pytest.raises(ValueError, match="cross-runtime"):
        report_tool(vendor_display_name=DISPLAY_NAME, name="acme.report")


@pytest.mark.parametrize("display_name", ["!!!", "FastMCP", "mcp-server", "A" * 2000])
def test_a_display_name_an_adapter_would_not_use_asks_for_a_name(display_name: str) -> None:
    with pytest.raises(ValueError, match="Pass name="):
        report_tool(vendor_display_name=display_name)


def test_an_empty_name_is_refused() -> None:
    with pytest.raises(ValueError, match="cross-runtime"):
        report_tool(vendor_display_name=DISPLAY_NAME, name="")


def test_each_tool_has_its_own_schema() -> None:
    _tool().input_schema["properties"].clear()

    assert _tool().input_schema["properties"]
