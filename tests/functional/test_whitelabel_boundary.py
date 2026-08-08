"""Whitelabel/boundary leak test (SPEC §5.4): no Baton-branded string ever
reaches the calling agent or end user. Installs with a vendor identity
clearly disjoint from "baton" (so a collision like "Batonic Corp" can't
produce a false positive) and case-insensitive-scans every agent/user-visible
surface the SPEC names: server instructions, the annotation tool's
description, injected param descriptions on a wrapped tool's advertised
schema, and the text a failed tool call actually returns to the caller.

Explicitly NOT scanned — exempt per SPEC §5.4's table, and scanning them
would be false positives: raised ``baton.errors.*`` exception class
names/messages, the ``User-Agent`` HTTP header on outbound Console calls,
Python import paths.

Verified against ``src/baton/integrations/_config.py``: ``VendorConfig`` has
no ``text_overrides``-style field today, so there is no
override-can't-reintroduce-the-string sub-case to test yet — nothing to
override.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from baton.sinks import FileSink

pytestmark = pytest.mark.functional

VENDOR_ID = "acme"
VENDOR_DISPLAY_NAME = "ACME Corp"


def _assert_no_baton_leak(surface_name: str, text: str | None) -> None:
    assert text is not None, f"{surface_name} was unexpectedly empty"
    assert "baton" not in text.lower(), f"{surface_name} leaks the Baton brand string: {text!r}"


def _assert_injected_param_descriptions_clean(surface_name: str, schema: dict[str, Any]) -> None:
    properties = schema.get("properties") or {}
    for param_name in ("user_goal", "expected_result"):
        if param_name in properties:
            _assert_no_baton_leak(
                f"{surface_name} injected param {param_name!r} description",
                properties[param_name].get("description"),
            )


async def test_mcp_adapter_surfaces_are_whitelabeled(tmp_path: Path) -> None:
    from baton.integrations.mcp import VendorConfig, install_baton
    from baton.integrations.mcp._compat import MCPServerClass as FastMCP

    mcp = FastMCP("whitelabel-mcp")

    @mcp.tool()
    def echo(text: str) -> dict[str, Any]:
        return {"text": text}

    @mcp.tool()
    def boom() -> None:
        raise ValueError("something broke")

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id=VENDOR_ID,
            vendor_display_name=VENDOR_DISPLAY_NAME,
            consent_token="ct_whitelabel",
            sink=FileSink(str(tmp_path / "events.jsonl")),
        ),
    )
    try:
        _assert_no_baton_leak("mcp.instructions", mcp.instructions)

        tools = await mcp.list_tools()
        annotate = next(t for t in tools if t.name == f"{VENDOR_ID}_annotate")
        _assert_no_baton_leak("annotate tool description", annotate.description)

        echo_tool = next(t for t in tools if t.name == "echo")
        schema = echo_tool.model_dump(by_alias=True)["inputSchema"]
        _assert_injected_param_descriptions_clean("mcp echo tool", schema)

        with pytest.raises(Exception) as excinfo:
            await mcp.call_tool("boom", {})
        _assert_no_baton_leak("mcp tool call exception text", str(excinfo.value))
    finally:
        await handle.aclose()


async def test_fastmcp_adapter_surfaces_are_whitelabeled(tmp_path: Path) -> None:
    from fastmcp import Client, FastMCP

    from baton.integrations.fastmcp import VendorConfig, install_baton

    mcp = FastMCP("whitelabel-fastmcp")

    @mcp.tool()
    def echo(text: str) -> dict[str, Any]:
        return {"text": text}

    @mcp.tool()
    def boom() -> None:
        raise ValueError("something broke")

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id=VENDOR_ID,
            vendor_display_name=VENDOR_DISPLAY_NAME,
            consent_token="ct_whitelabel",
            sink=FileSink(str(tmp_path / "events.jsonl")),
        ),
    )
    try:
        _assert_no_baton_leak("mcp.instructions", mcp.instructions)

        async with Client(mcp) as client:
            tools = await client.list_tools()
            annotate = next(t for t in tools if t.name == f"{VENDOR_ID}_annotate")
            _assert_no_baton_leak("annotate tool description", annotate.description)

            echo_tool = next(t for t in tools if t.name == "echo")
            _assert_injected_param_descriptions_clean("fastmcp echo tool", echo_tool.inputSchema)

            try:
                result = await client.call_tool("boom", {})
            except (
                Exception
            ) as exc:  # some fastmcp/mcp versions raise instead of returning is_error
                _assert_no_baton_leak("fastmcp tool call exception text", str(exc))
            else:
                assert result.is_error
                _assert_no_baton_leak("fastmcp tool call error result text", str(result.content))
    finally:
        await handle.aclose()
