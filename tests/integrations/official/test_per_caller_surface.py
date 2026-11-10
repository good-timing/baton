"""goal and reports per caller: the tool list and instructions (SPEC §2.6).

Here, under the official adapter, so the mcp 1.x CI legs run it; those have no
fastmcp, and the standalone driver joins only where it is installed."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest
from pydantic import BaseModel, RootModel

from baton import create_baton
from baton._capture_settings import CaptureSettings, parse_document
from baton.integrations import official
from baton.integrations._config import _validate_vendor_config, build_config
from baton.integrations._identity_adapter import ResolvedCaller
from baton.integrations._llm_text import (
    INTENT_INSTRUCTIONS,
    build_server_instructions,
    build_user_goal_param_description,
    drop_subagent_clause,
    write_ours,
)
from baton.integrations._per_caller import PerCallerSurface
from baton.integrations.official._compat import MCPServerClass as OfficialFastMCP
from baton.integrations.official._compat import get_lowlevel_server
from baton.sinks import FileSink
from tests._mcp_session import connected_session

try:
    from fastmcp import Client, FastMCP
except ImportError:
    FastMCP = None

DSN = "https://baton_pk_x@h.example.com/ten_" + "0" * 32 + "/srv"

REQUIRED = build_user_goal_param_description(intent_param_mode="required")
OPTIONAL = build_user_goal_param_description(intent_param_mode="optional")


def _document(*audiences: dict[str, Any]) -> bytes:
    return json.dumps({"version": 4, "audiences": list(audiences)}).encode()


def _audience(audience_id: str, match: dict[str, Any], **capture: str) -> dict[str, Any]:
    return {"id": audience_id, "match": match, "capture": capture}


# A caller from claude-code is in the beta audience; any other is not.
BETA_QUIET = _document(
    _audience("beta", {"client": ["claude-code"]}, goal="optional", reports="off"),
    _audience("everyone", {}, goal="required", reports="on"),
)
NOBODY_REPORTS = _document(_audience("everyone", {}, reports="off"))


class _Fetcher:
    def __init__(self, body: bytes | None) -> None:
        self.document = parse_document(body) if body is not None else None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _use_settings(monkeypatch: pytest.MonkeyPatch, settings: CaptureSettings) -> None:
    from baton.integrations import _shared

    monkeypatch.setattr(_shared, "build_capture_settings", lambda *_: settings)


def _settings(body: bytes | None) -> tuple[CaptureSettings, _Fetcher]:
    fetcher = _Fetcher(body)
    return CaptureSettings(vendor_id="srv", fetcher=fetcher), fetcher  # type: ignore[arg-type]


def _config(module: Any, tmp_path: Path) -> Any:
    return module.VendorConfig(
        vendor_id="srv",
        vendor_display_name="Server",
        consent_token="ct",
        tenant_id="ten_1",
        sink=FileSink(str(tmp_path / "e.jsonl")),
        report_mode="on",
    )


Listing = dict[str, dict[str, Any]]


def _schemas(tools: Sequence[Any]) -> Listing:
    return {t.name: t.model_dump(by_alias=True)["inputSchema"] for t in tools}


async def _official(mcp: Any, clients: Sequence[str]) -> tuple[list[Listing], list[str]]:
    listings, instructions = [], []
    for client in clients:
        async with connected_session(mcp, declared_name=client) as session:
            listings.append(_schemas((await session.list_tools()).tools))
            await session.call_tool("lookup", {"name": "alice"})
        instructions.append(_instructions(mcp))
    return listings, instructions


def _instructions(mcp: Any) -> str:
    return str(mcp.instructions)


def _lookup_server(make: Any) -> Any:
    mcp = make("per-caller")

    def lookup(name: str) -> dict[str, Any]:
        return {"name": name}

    if make is OfficialFastMCP:
        mcp.tool()(lookup)
    else:
        mcp.tool(lookup)
    return mcp


async def _drive_official(
    tmp_path: Path, clients: Sequence[str]
) -> tuple[list[Listing], list[str]]:
    mcp = _lookup_server(OfficialFastMCP)
    handle = official.install_baton(mcp, _config(official, tmp_path))
    try:
        return await _official(mcp, clients)
    finally:
        await handle.aclose()


async def _drive_standalone(
    tmp_path: Path, clients: Sequence[str]
) -> tuple[list[Listing], list[str]]:
    from baton.integrations import standalone

    mcp = _lookup_server(FastMCP)
    handle = standalone.install_baton(mcp, _config(standalone, tmp_path))
    listings, instructions = [], []
    try:
        for client in clients:
            declared = mcp_types.Implementation(name=client, version="1.0")
            async with Client(mcp, client_info=declared) as session:
                listings.append(_schemas(await session.list_tools()))
                await session.call_tool("lookup", {"name": "alice"})
            instructions.append(_instructions(mcp))
        return listings, instructions
    finally:
        await handle.aclose()


async def _drive_create_baton(
    tmp_path: Path, clients: Sequence[str]
) -> tuple[list[Listing], list[str]]:
    baton = create_baton(_config(official, tmp_path))
    try:
        mcp = _lookup_server(OfficialFastMCP)
        baton.wrap(mcp)
        return await _official(mcp, clients)
    finally:
        await baton.aclose()


DRIVERS = pytest.mark.parametrize(
    "drive",
    [_drive_official, _drive_create_baton] + ([_drive_standalone] if FastMCP else []),
)


def _report_tool(listing: Listing) -> list[str]:
    return [name for name in listing if name != "lookup"]


@DRIVERS
async def test_two_callers_in_different_audiences_see_different_tool_lists(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = _settings(BETA_QUIET)
    _use_settings(monkeypatch, settings)
    (beta, other), _ = await drive(tmp_path, ["claude-code", "cursor"])

    assert beta["lookup"]["properties"]["user_goal"]["description"] == OPTIONAL
    assert "user_goal" not in beta["lookup"].get("required", [])
    assert _report_tool(beta) == []

    assert other["lookup"]["properties"]["user_goal"]["description"] == REQUIRED
    assert "user_goal" in other["lookup"]["required"]
    assert len(_report_tool(other)) == 1


@DRIVERS
async def test_with_no_settings_held_the_list_is_the_codes(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = _settings(None)
    _use_settings(monkeypatch, settings)
    (listing,), (instructions,) = await drive(tmp_path, ["claude-code"])
    assert listing["lookup"]["properties"]["user_goal"]["description"] == REQUIRED
    assert len(_report_tool(listing)) == 1
    assert "to report a Server tool call" in instructions


@DRIVERS
async def test_the_pointer_leaves_the_instructions_when_nobody_has_reports(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = _settings(NOBODY_REPORTS)
    _use_settings(monkeypatch, settings)
    _, (instructions,) = await drive(tmp_path, ["claude-code"])
    assert instructions == INTENT_INSTRUCTIONS


async def test_a_server_built_after_the_settings_arrive_starts_without_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = _settings(NOBODY_REPORTS)
    _use_settings(monkeypatch, settings)
    baton = create_baton(_config(official, tmp_path))
    try:
        for _ in range(2):
            mcp = _lookup_server(OfficialFastMCP)
            baton.wrap(mcp)
            served = get_lowlevel_server(mcp).create_initialization_options().instructions
            assert served == INTENT_INSTRUCTIONS
    finally:
        await baton.aclose()


async def test_a_caller_without_reports_can_still_file_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = _settings(BETA_QUIET)
    _use_settings(monkeypatch, settings)
    mcp = _lookup_server(OfficialFastMCP)
    handle = official.install_baton(mcp, _config(official, tmp_path))
    try:
        async with connected_session(mcp, declared_name="claude-code") as session:
            listed = (await session.list_tools()).tools
            assert handle.annotation_tool_name not in [t.name for t in listed]
            result = await session.call_tool(
                handle.annotation_tool_name,
                {"tool_name": "lookup", "user_goal": "1: find alice; success is her row"},
            )
        assert not result.model_dump(by_alias=True)["isError"]
    finally:
        await handle.aclose()


class _Server:
    def __init__(self, instructions: str) -> None:
        self.instructions = instructions


def _write(server: Any, text: str) -> None:
    server.instructions = text


def _surface(server: _Server, settings: CaptureSettings) -> PerCallerSurface:
    return PerCallerSurface(
        mcp=server,
        settings=settings,
        intent_param_mode="required",
        annotation_tool_name="srv_annotate",
        pointer=POINTER,
        write_instructions=_write,
        warned=set(),
    )


OURS = build_server_instructions(vendor_display_name="Server", annotation_tool_name="srv_annotate")
POINTER = OURS.removesuffix(f"\n\n{INTENT_INSTRUCTIONS}")


def _installed(vendor: str) -> _Server:
    server = _Server(vendor)
    write_ours(server, OURS, _assign)
    return server


class TestInstructions:
    def test_they_follow_the_held_document_both_ways(self) -> None:
        settings, fetcher = _settings(NOBODY_REPORTS)
        server = _installed("Vendor text.")
        surface = _surface(server, settings)

        surface.follow_settings()
        assert server.instructions == f"Vendor text.\n\n{INTENT_INSTRUCTIONS}"

        fetcher.document = None  # the collector answered 204
        surface.follow_settings()
        assert server.instructions == f"Vendor text.\n\n{OURS}"

        fetcher.document = parse_document(BETA_QUIET)
        surface.follow_settings()
        assert server.instructions == f"Vendor text.\n\n{OURS}"

    def test_a_removed_subagent_sentence_stays_removed(self) -> None:
        settings, fetcher = _settings(NOBODY_REPORTS)
        server = _installed("Vendor text.")
        drop_subagent_clause(server, "lookup", _write)
        surface = _surface(server, settings)

        surface.follow_settings()
        assert server.instructions == "Vendor text."
        fetcher.document = None
        surface.follow_settings()
        assert server.instructions == f"Vendor text.\n\n{POINTER}"

    def test_text_the_vendor_sets_later_keeps_the_subagent_sentence(self) -> None:
        settings, fetcher = _settings(NOBODY_REPORTS)
        server = _installed("Vendor text.")
        surface = _surface(server, settings)
        server.instructions = "Later."

        surface.follow_settings()
        assert server.instructions == f"Later.\n\n{INTENT_INSTRUCTIONS}"
        fetcher.document = None
        surface.follow_settings()
        assert server.instructions == f"Later.\n\n{OURS}"

    def test_a_dropped_catch_all_leaves_the_codes_reports_reachable(self) -> None:
        body = _document(
            _audience("beta", {"label": ["beta"]}, reports="off"),
            _audience("odd", {"plan": ["pro"]}, reports="off"),
        )
        document = parse_document(body)
        assert document is not None
        assert document.reports_on_for_anyone("on")
        assert not document.reports_on_for_anyone("off")


class _Tool(BaseModel):
    name: str
    input_schema: dict[str, Any]


class _Listed(BaseModel):
    tools: list[_Tool]


class _Wrapped(RootModel[_Listed]):
    pass


def _caller(body: bytes, client: str) -> ResolvedCaller:
    from baton._capture_settings import Caller

    document = parse_document(body)
    assert document is not None
    return ResolvedCaller(settings=document.settings_for(Caller(client_name=client), "srv"))


def _listed(schema: dict[str, Any]) -> mcp_types.ListToolsResult:
    tool = mcp_types.Tool.model_validate({"name": "lookup", "inputSchema": schema})
    return mcp_types.ListToolsResult(tools=[tool])


class TestToolsFor:
    def _surface(self) -> PerCallerSurface:
        settings, _ = _settings(BETA_QUIET)
        return _surface(_Server(""), settings)

    def _goal_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"name": {"type": "string"}, "user_goal": {"description": REQUIRED}},
            "required": ["name", "user_goal"],
        }

    def test_a_wrapped_result_keeps_its_type(self) -> None:
        result = _Wrapped(
            _Listed(
                tools=[
                    _Tool(name="lookup", input_schema=self._goal_schema()),
                    _Tool(name="srv_annotate", input_schema={}),
                ]
            )
        )
        out = self._surface().tools_for(result, _caller(BETA_QUIET, "claude-code"))
        assert isinstance(out, _Wrapped)
        (tool,) = out.root.tools
        assert tool.input_schema["required"] == ["name"]
        assert tool.input_schema["properties"]["user_goal"]["description"] == OPTIONAL
        assert result.root.tools[0].input_schema["required"] == ["name", "user_goal"]

    def test_a_tools_own_user_goal_is_left_as_it_is(self) -> None:
        own = {
            "properties": {"user_goal": {"description": "the vendor's"}},
            "required": ["user_goal"],
        }
        out = self._surface().tools_for(_listed(own), _caller(BETA_QUIET, "claude-code"))
        assert _schemas(out.tools)["lookup"] == own

    def test_a_schema_it_cannot_read_is_left_as_it_is(self) -> None:
        odd = {"properties": {"user_goal": 3}}
        out = self._surface().tools_for(_listed(odd), _caller(BETA_QUIET, "claude-code"))
        assert _schemas(out.tools)["lookup"] == odd

    def test_a_caller_on_the_codes_settings_gets_the_result_unchanged(self) -> None:
        result = object()
        assert self._surface().tools_for(result, _caller(BETA_QUIET, "cursor")) is result
        assert self._surface().tools_for(result, ResolvedCaller()) is result


class TestReportModeDefault:
    def test_console_mode_turns_reports_on(self) -> None:
        config = build_config(official.VendorConfig(dsn=DSN, settings="console"), None)
        assert config.report_mode == "on"

    def test_a_vendor_who_turns_reports_off_keeps_them_off(self) -> None:
        given = official.VendorConfig(dsn=DSN, settings="console", report_mode="off")
        assert build_config(given, None).report_mode == "off"

    def test_a_dsn_from_the_environment_turns_reports_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BATON_DSN", DSN)
        config = build_config(official.VendorConfig(settings="console"), None)
        assert config.report_mode == "on"

    def test_console_mode_without_a_dsn_says_it_needs_one(self) -> None:
        given = official.VendorConfig(vendor_id="acme", tenant_id="t", settings="console")
        with pytest.raises(ValueError, match="so it needs a dsn"):
            _validate_vendor_config(build_config(given, None))

    def test_local_mode_keeps_reports_off(self) -> None:
        assert build_config(official.VendorConfig(dsn=DSN), None).report_mode == "off"


def _assign(mcp: Any, text: str) -> None:
    mcp.instructions = text
