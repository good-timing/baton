"""Overlays from the settings document, applied by both adapters (SPEC §2.6).

Here, under the official adapter, so the mcp 1.x CI legs run it; the
standalone driver joins only where fastmcp is installed."""

from __future__ import annotations

import json
import logging
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

import mcp.types as mcp_types
import pytest

from baton._capture_settings import CaptureSettings, parse_document
from baton._overlays import Action, changed_text
from baton.integrations import official
from baton.integrations._llm_text import INTENT_INSTRUCTIONS, build_refusal_text, write_ours
from baton.integrations.official._compat import MCPServerClass as OfficialFastMCP
from baton.sinks import FileSink
from tests._event_helpers import read_events
from tests._mcp_session import connected_session

try:
    from fastmcp import Client, FastMCP
except ImportError:
    FastMCP = None

GOAL = "1: book a slot; success is a booking id"

# The document the Console sent for C1 (Console main f7f5055c), with ``beta``
# matched by client so a test can pick its audience.
CONSOLE_SAMPLE = {
    "version": 9,
    "audiences": [
        {
            "id": "beta",
            "match": {"label": ["beta"], "share": 0.25},
            "capture": {"goal": "required", "reports": "on", "results": "full"},
            "overlay": "ov_beta_1",
        },
        {
            "id": "everyone",
            "match": {},
            "capture": {"goal": "required", "reports": "on", "results": "off"},
        },
    ],
    "overlays": {
        "ov_beta_1": {
            "actions": [
                {
                    "target": {"tool": "book_slot"},
                    "replace": "Books one slot. Check availability first.",
                },
                {"target": {"tool": "find_slots"}, "append": "Returns at most 20 slots."},
                {"target": {"tool": "legacy_book"}, "remove": True},
                {
                    "target": {"tool": "book_slot", "param": "timezone"},
                    "append": "Use an IANA name, such as Europe/Paris.",
                },
                {"target": {"tool": "book_slot", "param": "notes"}, "remove": True},
                {"target": {"tool": "find_slots", "param": "duration"}, "required": True},
                {
                    "target": {"tool": "find_slots", "param": "day"},
                    "replace": "The day, as YYYY-MM-DD.",
                },
                {
                    "target": {"agent_text": "goal_param.description"},
                    "replace": "What the user wants, in their words.",
                },
                {
                    "target": {"agent_text": "report_tool.description"},
                    "append": "Use it when a booking fails twice.",
                },
                {
                    "target": {"agent_text": "report_tool.refusal"},
                    "replace": "Reports are off for now.",
                },
            ]
        },
        "ov_server_1": {
            "actions": [
                {
                    "target": {"instructions": True},
                    "append": "Times are in the user's timezone.",
                },
                {
                    "target": {"agent_text": "instructions.report_pointer"},
                    "replace": "If a booking keeps failing, say so with the report tool.",
                },
                {
                    "target": {"agent_text": "instructions.subagent"},
                    "append": "Pass the user's timezone to subagents.",
                },
            ]
        },
    },
    "server_overlay": "ov_server_1",
}


def _document(**changes: Any) -> dict[str, Any]:
    document = json.loads(json.dumps(CONSOLE_SAMPLE))
    document["audiences"][0]["match"] = {"client": ["claude-code"]}
    document.update(changes)
    return document


class _Fetcher:
    def __init__(self, document: dict[str, Any] | None) -> None:
        self.document = None if document is None else parse_document(json.dumps(document).encode())

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _use(monkeypatch: pytest.MonkeyPatch, document: dict[str, Any] | None) -> _Fetcher:
    from baton.integrations import _shared

    fetcher = _Fetcher(document)
    settings = CaptureSettings(vendor_id="srv", fetcher=fetcher)  # type: ignore[arg-type]
    monkeypatch.setattr(_shared, "build_capture_settings", lambda *_: settings)
    return fetcher


def _config(module: Any, tmp_path: Path) -> Any:
    return module.VendorConfig(
        vendor_id="srv",
        vendor_display_name="Server",
        consent_token="ct",
        tenant_id="ten_1",
        sink=FileSink(str(tmp_path / "e.jsonl")),
        report_mode="on",
    )


def book_slot(day: str, timezone: str, notes: str = "") -> str:
    """Book a slot."""
    return f"booked {day}"


def find_slots(day: str, duration: int | None = None) -> str:
    """Find open slots."""
    return f"slots on {day}"


def legacy_book(day: str) -> str:
    """Book a slot, the old way."""
    return f"legacy {day}"


def _server(make: Any) -> Any:
    mcp = make("booking")
    for tool in (book_slot, find_slots, legacy_book):
        if make is OfficialFastMCP:
            mcp.tool()(tool)
        else:
            mcp.tool(tool)
    return mcp


class Seen(NamedTuple):
    tools: dict[str, dict[str, Any]]
    calls: dict[str, dict[str, Any]]
    instructions: str | None


def _dumped(result: Any) -> dict[str, Any]:
    return dict(result.model_dump(mode="json", by_alias=True, exclude_none=True))


REPORT_TOOL = "<report tool>"

CALLS = {
    "book": ("book_slot", {"day": "mon", "timezone": "UTC", "user_goal": GOAL}),
    "legacy": ("legacy_book", {"day": "mon", "user_goal": GOAL}),
    "missing": ("ghost_book", {"day": "mon", "user_goal": GOAL}),
    "find": ("find_slots", {"day": "mon", "user_goal": GOAL}),
}


def _named(call: tuple[str, dict[str, Any]], handle: Any) -> tuple[str, dict[str, Any]]:
    name, arguments = call
    return (handle.annotation_tool_name if name == REPORT_TOOL else name), arguments


@pytest.fixture(autouse=True)
def _warn_again() -> Any:
    from baton import _overlays

    yield
    _overlays._warned.clear()
    _overlays._unreadable.clear()


async def _official_drive(
    tmp_path: Path, clients: Sequence[str]
) -> tuple[list[Seen], list[dict[str, Any]], Any]:
    mcp = _server(OfficialFastMCP)
    handle = official.install_baton(mcp, _config(official, tmp_path))
    seen = []
    try:
        for client in clients:
            async with connected_session(mcp, declared_name=client) as session:
                listed = (await session.list_tools()).tools
                calls = {
                    k: _dumped(await session.call_tool(*_named(v, handle)))
                    for k, v in CALLS.items()
                }
                seen.append(
                    Seen(
                        {t.name: _dumped(t) for t in listed},
                        calls,
                        str(mcp.instructions),
                    )
                )
    finally:
        await handle.aclose()
    return seen, read_events(tmp_path / "e.jsonl"), handle


async def _standalone_drive(
    tmp_path: Path, clients: Sequence[str]
) -> tuple[list[Seen], list[dict[str, Any]], Any]:
    from baton.integrations import standalone

    mcp = _server(FastMCP)
    handle = standalone.install_baton(mcp, _config(standalone, tmp_path))
    seen = []
    try:
        for client in clients:
            declared = mcp_types.Implementation(name=client, version="1.0")
            async with Client(mcp, client_info=declared) as session:
                listed = await session.list_tools()
                calls = {
                    k: _dumped(await session.call_tool_mcp(*_named(v, handle)))
                    for k, v in CALLS.items()
                }
                seen.append(
                    Seen({t.name: _dumped(t) for t in listed}, calls, str(mcp.instructions))
                )
    finally:
        await handle.aclose()
    return seen, read_events(tmp_path / "e.jsonl"), handle


DRIVERS = pytest.mark.parametrize(
    "drive", [_official_drive] + ([_standalone_drive] if FastMCP else [])
)


def _props(seen: Seen, tool: str) -> dict[str, Any]:
    return dict(seen.tools[tool]["inputSchema"]["properties"])


def _required(seen: Seen, tool: str) -> list[str]:
    return list(seen.tools[tool]["inputSchema"].get("required", []))


def _report_tool(seen: Seen) -> str:
    (name,) = set(seen.tools) - {"book_slot", "find_slots", "legacy_book"}
    return name


@DRIVERS
async def test_two_callers_in_different_audiences_see_different_tool_lists(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _document())
    (beta, other), _, _ = await drive(tmp_path, ["claude-code", "cursor"])

    assert "legacy_book" not in beta.tools
    assert "legacy_book" in other.tools
    assert beta.tools["book_slot"]["description"] == "Books one slot. Check availability first."
    assert other.tools["book_slot"]["description"] == "Book a slot."
    assert beta.tools["find_slots"]["description"] == (
        "Find open slots.\n\nReturns at most 20 slots."
    )
    assert _props(beta, "book_slot")["timezone"]["description"] == (
        "Use an IANA name, such as Europe/Paris."
    )
    assert "notes" not in _props(beta, "book_slot")
    assert "notes" in _props(other, "book_slot")
    assert _props(beta, "find_slots")["day"]["description"] == "The day, as YYYY-MM-DD."
    assert "duration" in _required(beta, "find_slots")
    assert "duration" not in _required(other, "find_slots")
    for tool in ("book_slot", "find_slots"):
        assert _props(beta, tool)["user_goal"]["description"] == (
            "What the user wants, in their words."
        )
        assert _props(other, tool)["user_goal"]["description"].startswith("REQUIRED.")
    assert beta.tools[_report_tool(beta)]["description"].endswith(
        "\n\nUse it when a booking fails twice."
    )
    assert not other.tools[_report_tool(other)]["description"].endswith("fails twice.")


@DRIVERS
async def test_a_hidden_tool_answers_as_a_missing_one(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _document())
    (beta, other), events, _ = await drive(tmp_path, ["claude-code", "cursor"])

    missing = json.loads(json.dumps(beta.calls["missing"]).replace("ghost_book", "legacy_book"))
    assert beta.calls["legacy"] == missing
    assert beta.calls["legacy"]["content"][0]["text"].startswith("Unknown tool: ")
    assert other.calls["legacy"]["content"][0]["text"] == "legacy mon"

    hidden = [
        e
        for e in events
        if e["event_type"] == "tool_call_error" and e["payload"]["tool_name"] == "legacy_book"
    ]
    (error,) = hidden
    assert error["payload"]["failure_kind"] == "tool_hidden"
    assert error["payload"]["error_body"] == missing["content"][0]["text"]
    assert error["capture"] == {"settings_version": 9, "audience": "beta", "overlay": "ov_beta_1"}
    (start,) = [
        e
        for e in events
        if e["event_type"] == "tool_call_start" and e["call_id"] == error["call_id"]
    ]
    assert start["payload"]["call_intent"] == GOAL
    assert "user_goal" not in start["payload"]["params"]


@DRIVERS
async def test_a_param_listed_as_required_is_not_enforced(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _document())
    (beta,), _, _ = await drive(tmp_path, ["claude-code"])
    assert "duration" in _required(beta, "find_slots")
    assert beta.calls["find"]["content"][0]["text"] == "slots on mon"


@DRIVERS
async def test_every_event_in_an_audience_with_an_overlay_carries_it(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _document())
    _, events, _ = await drive(tmp_path, ["claude-code"])
    per_call = [e for e in events if e["event_type"] != "surface_snapshot"]
    assert per_call
    assert all(e["capture"]["overlay"] == "ov_beta_1" for e in per_call)


@DRIVERS
async def test_the_server_overlay_is_in_the_instructions(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _document())
    (beta,), _, _ = await drive(tmp_path, ["claude-code"])
    assert beta.instructions == (
        "Times are in the user's timezone.\n\n"
        "If a booking keeps failing, say so with the report tool.\n\n"
        f"{INTENT_INSTRUCTIONS}\n\nPass the user's timezone to subagents."
    )


@DRIVERS
async def test_the_snapshot_names_the_server_overlay(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _document())
    _, events, _ = await drive(tmp_path, ["claude-code"])
    (snapshot,) = [e for e in events if e["event_type"] == "surface_snapshot"]
    seam = snapshot["payload"]["seam_augmentations"]
    assert seam["server_overlay"] == "ov_server_1"
    assert seam["agent_text"]["instructions.report_pointer"] == {
        "text": "If a booking keeps failing, say so with the report tool.",
        "source": "replaced",
    }
    assert seam["agent_text"]["instructions.subagent"] == {
        "text": f"{INTENT_INSTRUCTIONS}\n\nPass the user's timezone to subagents.",
        "source": "appended",
    }
    assert seam["agent_text"]["goal_param.description"]["source"] == "default"


@DRIVERS
async def test_with_no_server_overlay_the_snapshot_sends_null(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _document()
    del document["server_overlay"]
    _use(monkeypatch, document)
    _, events, _ = await drive(tmp_path, ["claude-code"])
    (snapshot,) = [e for e in events if e["event_type"] == "surface_snapshot"]
    assert snapshot["payload"]["seam_augmentations"]["server_overlay"] is None


@DRIVERS
async def test_without_console_settings_the_snapshot_leaves_server_overlay_out(
    drive: Any, tmp_path: Path
) -> None:
    _, events, _ = await drive(tmp_path, ["claude-code"])
    (snapshot,) = [e for e in events if e["event_type"] == "surface_snapshot"]
    assert "server_overlay" not in snapshot["payload"]["seam_augmentations"]


async def test_a_new_server_overlay_sends_a_new_snapshot_and_the_same_one_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _use(monkeypatch, _document())
    mcp = _server(OfficialFastMCP)
    handle = official.install_baton(mcp, _config(official, tmp_path))
    renamed = _document(server_overlay="ov_server_2")
    renamed["overlays"]["ov_server_2"] = renamed["overlays"]["ov_server_1"]
    try:
        async with connected_session(mcp, declared_name="cursor") as session:
            await session.call_tool(*CALLS["book"])
            fetcher.document = parse_document(json.dumps(_document()).encode())
            await session.call_tool(*CALLS["book"])
            fetcher.document = parse_document(json.dumps(renamed).encode())
            await session.call_tool(*CALLS["book"])
            await session.call_tool(*CALLS["book"])
            fetcher.document = parse_document(json.dumps(_document()).encode())
            await session.call_tool(*CALLS["book"])
    finally:
        await handle.aclose()
    snapshots = [
        e["payload"]["seam_augmentations"]["server_overlay"]
        for e in read_events(tmp_path / "e.jsonl")
        if e["event_type"] == "surface_snapshot"
    ]
    assert snapshots == ["ov_server_1", "ov_server_2", "ov_server_1"]


@DRIVERS
async def test_the_refusal_follows_the_callers_overlay(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _document())
    calls = dict(CALLS)
    CALLS.clear()
    CALLS["refused"] = (REPORT_TOOL, {"user_goal": GOAL})
    try:
        (beta, other), _, _ = await drive(tmp_path, ["claude-code", "cursor"])
    finally:
        CALLS.clear()
        CALLS.update(calls)
    assert "Reports are off for now." in json.dumps(beta.calls["refused"])
    assert "reactive-only" in json.dumps(other.calls["refused"])


@DRIVERS
async def test_an_action_that_cannot_apply_is_skipped_alone(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    document = _document()
    document["overlays"]["ov_beta_1"]["actions"][:0] = [
        {"target": {"tool": "nope"}, "append": "x"},
        {"target": {"tool": "book_slot", "param": "day"}, "remove": True},
        {"target": {"instructions": True}, "append": "server only"},
    ]
    _use(monkeypatch, document)
    with caplog.at_level(logging.WARNING, logger="baton"):
        (beta,), _, _ = await drive(tmp_path, ["claude-code"])
    assert "day" in _props(beta, "book_slot")
    assert "notes" not in _props(beta, "book_slot")
    skipped = [r.getMessage() for r in caplog.records if "is skipped" in r.getMessage()]
    assert any("no tool is named 'nope'" in m for m in skipped)
    assert any("requires 'day'" in m for m in skipped)
    assert any("goes only in server_overlay" in m for m in skipped)


class TestReadingOverlays:
    def test_the_console_sample_reads_whole(self) -> None:
        document = parse_document(json.dumps(CONSOLE_SAMPLE).encode())
        assert document is not None
        beta = document.audiences[0].settings
        assert beta.capture.overlay == "ov_beta_1"
        assert beta.overlay is not None
        assert len(beta.overlay.actions) == 10
        assert beta.overlay.hidden_tools == {"legacy_book"}
        assert document.server_overlay is not None
        assert len(document.server_overlay.actions) == 3
        assert document.audiences[1].settings.overlay is None

    @pytest.mark.parametrize("overlays", [{}, None, "not an object"])
    def test_an_unknown_overlay_leaves_the_document_valid(self, overlays: Any) -> None:
        body = _document(overlays=overlays)
        document = parse_document(json.dumps(body).encode())
        assert document is not None
        assert document.audiences[0].settings.overlay is None
        assert document.audiences[0].settings.capture.overlay is None
        assert document.server_overlay is None

    def test_overlays_absent_reads_as_none(self) -> None:
        body = _document()
        del body["overlays"], body["server_overlay"], body["audiences"][0]["overlay"]
        document = parse_document(json.dumps(body).encode())
        assert document is not None
        assert document.server_overlay is None
        assert document.audiences[0].settings.capture.overlay is None


def test_an_append_starts_a_new_paragraph_after_trailing_space() -> None:
    action = Action(0, "append", "Added.")
    assert changed_text("Line one.\n- item\n", (action,)) == (
        "Line one.\n- item\n\nAdded.",
        "appended",
    )
    assert changed_text(None, (action,)) == ("Added.", "appended")
    replaced = Action(1, "replace", "New.")
    assert changed_text("Old.", (replaced, action)) == ("New.\n\nAdded.", "replaced")


def test_the_refusal_text_takes_the_overlay() -> None:
    document = parse_document(json.dumps(CONSOLE_SAMPLE).encode())
    assert document is not None
    overlay = document.audiences[0].settings.overlay
    assert (
        build_refusal_text(annotation_tool_name="a", overlay=overlay) == "Reports are off for now."
    )


async def test_checking_for_a_hidden_tool_asks_the_hook_once_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _document())
    asked: list[str | None] = []

    def resolve_principal(context: Any) -> None:
        asked.append(context.tool_name)

    mcp = _server(OfficialFastMCP)
    config = _config(official, tmp_path)
    config = official.VendorConfig(**{**vars(config), "resolve_principal": resolve_principal})
    handle = official.install_baton(mcp, config)
    try:
        async with connected_session(mcp, declared_name="cursor") as session:
            await session.call_tool(*CALLS["legacy"])
    finally:
        await handle.aclose()
    # The client lists the tools itself after a call, which asks once more.
    assert asked.count("legacy_book") == 1


@DRIVERS
async def test_a_tool_the_server_lacks_is_unknown_where_the_adapter_can_tell(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The standalone adapter cannot tell a missing tool from a failed lookup,
    and a failed lookup must not serve a hidden tool, so there it stays hidden."""
    document = _document()
    document["overlays"]["ov_beta_1"]["actions"].append(
        {"target": {"tool": "ghost_book"}, "remove": True}
    )
    _use(monkeypatch, document)
    (beta,), events, _ = await drive(tmp_path, ["claude-code"])
    (error,) = [
        e
        for e in events
        if e["event_type"] == "tool_call_error" and e["payload"]["tool_name"] == "ghost_book"
    ]
    expected = "unknown_tool" if drive is _official_drive else "tool_hidden"
    assert error["payload"]["failure_kind"] == expected
    assert beta.calls["missing"]["content"][0]["text"].startswith("Unknown tool: ")


@DRIVERS
async def test_instruction_text_is_trimmed_and_an_empty_replace_removes_it(
    drive: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _document()
    document["overlays"]["ov_server_1"]["actions"] = [
        {"target": {"instructions": True}, "append": "Times are local.\n"},
        {"target": {"agent_text": "instructions.subagent"}, "replace": ""},
    ]
    _use(monkeypatch, document)
    (beta,), _, _ = await drive(tmp_path, ["claude-code"])
    assert beta.instructions.startswith("Times are local.\n\nUse `")
    assert "\n\n\n" not in beta.instructions
    assert INTENT_INSTRUCTIONS not in beta.instructions


def test_a_failed_instructions_write_is_tried_again() -> None:
    from baton.integrations._per_caller import PerCallerSurface

    fetcher = _Fetcher(_document())
    settings = CaptureSettings(vendor_id="srv", fetcher=fetcher)  # type: ignore[arg-type]
    server = types.SimpleNamespace(instructions="Vendor.")
    write_ours(server, f"Use `srv_annotate`.\n\n{INTENT_INSTRUCTIONS}", _assign)
    failures = [OSError("layout")]

    def write(mcp: Any, text: str) -> None:
        if failures:
            raise failures.pop()
        mcp.instructions = text

    surface = PerCallerSurface(
        mcp=server,
        settings=settings,
        intent_param_mode="required",
        annotation_tool_name="srv_annotate",
        pointer="Use `srv_annotate`.",
        write_instructions=write,
        warned=set(),
    )
    surface.follow_settings()
    assert server.instructions == f"Vendor.\n\nUse `srv_annotate`.\n\n{INTENT_INSTRUCTIONS}"
    surface.follow_settings()
    assert server.instructions == (
        "Vendor.\n\nTimes are in the user's timezone.\n\n"
        "If a booking keeps failing, say so with the report tool.\n\n"
        f"{INTENT_INSTRUCTIONS}\n\nPass the user's timezone to subagents."
    )


def _assign(mcp: Any, text: str) -> None:
    mcp.instructions = text


def test_a_surface_sent_under_another_server_overlay_is_sent_again() -> None:
    from baton.integrations._surface import SentSurfaces

    sent = SentSurfaces()
    sent.mark("d1", "ov_a")
    sent.mark("d2", "ov_b")
    assert not sent.was_sent("d1", "ov_a")
    sent.mark("d1", "ov_a")
    assert sent.was_sent("d1", "ov_a")
    assert not sent.was_sent("d2", "ov_a")
