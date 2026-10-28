"""``VendorConfig.report_mode`` on both MCP adapters: off by default, the
annotation tool and its instructions only when it is ``"on"``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from baton.integrations._llm_text import (
    INTENT_INSTRUCTIONS,
    build_server_instructions,
    build_user_goal_param_description,
)
from tests._forced_reorder import CapturingSink

pytestmark = pytest.mark.functional


@dataclass
class Served:
    mcp: Any
    handle: Any
    sink: CapturingSink
    schemas: dict[str, dict[str, Any]]
    call: Callable[[str, dict[str, Any]], Any]

    @property
    def instructions(self) -> str:
        return self.mcp.instructions or ""


def _config(sink: CapturingSink, overrides: dict[str, Any]) -> dict[str, Any]:
    return {
        "vendor_id": "v",
        "consent_token": "ct_v",
        "vendor_display_name": "Toybox",
        "sink": sink,
        **overrides,
    }


@asynccontextmanager
async def _official(
    tools: list[Callable[..., Any]],
    instructions: str | None = None,
    installs: int = 1,
    **config: Any,
) -> AsyncIterator[Served]:
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass
    from tests._mcp_session import connected_session

    mcp = MCPServerClass("toybox", instructions=instructions)
    for tool in tools:
        mcp.add_tool(tool)
    sink = CapturingSink()
    for _ in range(installs):
        handle = install_baton(
            mcp,
            VendorConfig(**_config(sink, config)),
        )
    try:
        async with connected_session(mcp) as session:
            listed = (await session.list_tools()).tools
            schemas = {t.name: getattr(t, "input_schema", None) or t.inputSchema for t in listed}
            yield Served(mcp, handle, sink, schemas, session.call_tool)
    finally:
        await handle.aclose()


@asynccontextmanager
async def _standalone(
    tools: list[Callable[..., Any]], instructions: str | None = None, **config: Any
) -> AsyncIterator[Served]:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton

    mcp = FastMCP("toybox", instructions=instructions)
    for tool in tools:
        mcp.tool(tool)
    sink = CapturingSink()
    handle = install_baton(
        mcp,
        VendorConfig(**_config(sink, config)),
    )
    try:
        async with Client(mcp) as client:
            listed = await client.list_tools()
            schemas = {t.name: getattr(t, "input_schema", None) or t.inputSchema for t in listed}
            yield Served(mcp, handle, sink, schemas, client.call_tool)
    finally:
        await handle.aclose()


both_adapters = pytest.mark.parametrize(
    "serve", [_official, _standalone], ids=["official", "standalone"]
)


def lookup(name: str) -> str:
    return f"found {name}"


def toybox_annotate(note: str) -> str:
    """A vendor tool that happens to have the annotation tool's default name."""
    return note


def approve(doc: str, user_goal: str = "") -> str:
    return doc


@both_adapters
async def test_by_default_no_annotation_tool_and_only_the_subagent_sentence(serve: Any) -> None:
    async with serve([lookup]) as served:
        assert set(served.schemas) == {"lookup"}
        assert served.instructions == INTENT_INSTRUCTIONS
        assert served.handle.annotation_tool_name == ""


@both_adapters
async def test_user_goal_is_injected_as_before_with_reports_off(serve: Any) -> None:
    async with serve([lookup]) as served:
        injected = served.schemas["lookup"]["properties"]["user_goal"]
        assert injected["description"] == build_user_goal_param_description(
            intent_param_mode="required"
        )
        assert "user_goal" in served.schemas["lookup"]["required"]


@both_adapters
async def test_the_vendors_instructions_stay_above_the_sentence(serve: Any) -> None:
    async with serve([lookup], instructions="Use lookup for names.") as served:
        assert served.instructions == f"Use lookup for names.\n\n{INTENT_INSTRUCTIONS}"


async def test_a_second_install_replaces_the_sentence_rather_than_adding_one() -> None:
    async with _official([lookup], instructions="Use lookup for names.", installs=2) as served:
        assert served.instructions == f"Use lookup for names.\n\n{INTENT_INSTRUCTIONS}"


@both_adapters
async def test_with_reports_off_a_tool_named_like_the_annotation_tool_is_captured(
    serve: Any,
) -> None:
    async with serve([toybox_annotate]) as served:
        await served.call("toybox_annotate", {"note": "hi", "user_goal": "1: note; success is ok"})
        starts = [e for e in served.sink.events if e.event_type == "tool_call_start"]
        assert [e.payload.tool_name for e in starts] == ["toybox_annotate"]


@both_adapters
async def test_report_mode_on_serves_the_annotation_tool_and_its_instructions(serve: Any) -> None:
    async with serve([lookup], report_mode="on") as served:
        assert set(served.schemas) == {"lookup", "toybox_annotate"}
        assert served.handle.annotation_tool_name == "toybox_annotate"
        assert served.instructions == build_server_instructions(
            vendor_display_name="Toybox", annotation_tool_name="toybox_annotate"
        )


@both_adapters
@pytest.mark.parametrize(
    "setting", [{"proactive_mode": "on"}, {"annotation_tool_name": "acme_report"}], ids=str
)
async def test_an_annotation_setting_alone_warns_and_changes_nothing(
    serve: Any, setting: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="baton"):
        async with serve([lookup], **setting) as served:
            assert set(served.schemas) == {"lookup"}
            assert served.instructions == INTENT_INSTRUCTIONS
    (field,) = setting
    assert f"{field}={setting[field]!r} has no effect with report_mode='off'" in caplog.text


@both_adapters
@pytest.mark.parametrize("vendor_text", [None, "Use approve for docs."])
async def test_a_tool_with_its_own_user_goal_removes_the_sentence(
    serve: Any, vendor_text: str | None
) -> None:
    """The sentence would send the turn number into the vendor's argument; with
    reports off it is all of Baton's text, so nothing of ours is left."""
    async with serve([approve], instructions=vendor_text) as served:
        await served.call("approve", {"doc": "d"})
        assert served.instructions == (vendor_text or "")
        assert _seam(served)["instructions_suffix"] is False


@both_adapters
@pytest.mark.parametrize("report_mode", ["off", "on"])
async def test_the_snapshot_says_what_baton_added(serve: Any, report_mode: str) -> None:
    async with serve([lookup], report_mode=report_mode) as served:
        await served.call("lookup", {"name": "a"})
        seam = _seam(served)
    assert seam["instructions_suffix"] is True
    assert seam["injected_tools"] == ([] if report_mode == "off" else ["toybox_annotate"])


@both_adapters
async def test_reports_on_keep_the_sentence_beside_a_native_user_goal(serve: Any) -> None:
    async with serve([approve], report_mode="on") as served:
        await served.call("approve", {"doc": "d"})
        assert "subagent" not in served.instructions
        assert _seam(served)["instructions_suffix"] is True


def _seam(served: Served) -> dict[str, Any]:
    (snapshot,) = [e for e in served.sink.events if e.event_type == "surface_snapshot"]
    return dict(snapshot.payload.seam_augmentations)


def test_a_display_name_is_needed_only_with_reports_on() -> None:
    from baton.integrations._config import VendorConfig, _validate_vendor_config

    _validate_vendor_config(VendorConfig(vendor_id="v"))
    with pytest.raises(ValueError, match="vendor_display_name with report_mode='on'"):
        _validate_vendor_config(VendorConfig(vendor_id="v", report_mode="on"))


def test_intent_capture_needs_injection_or_proactive_reports() -> None:
    from baton.integrations._config import VendorConfig, _validate_vendor_config

    def config(**modes: str) -> VendorConfig:
        return VendorConfig(vendor_id="v", vendor_display_name="V", **modes)

    _validate_vendor_config(config(intent_param_mode="off", report_mode="on", proactive_mode="on"))
    for modes in (
        {"intent_param_mode": "off"},
        {"intent_param_mode": "off", "proactive_mode": "on"},
        {"intent_param_mode": "off", "report_mode": "on"},
    ):
        with pytest.raises(ValueError, match="nothing would capture"):
            _validate_vendor_config(config(**modes))
    with pytest.raises(ValueError, match="report_mode 'yes' must be one of"):
        _validate_vendor_config(config(report_mode="yes"))
