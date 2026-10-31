"""The library's ``list_tools`` and ``INTENT_INSTRUCTIONS``: what a vendor
serving MCP itself hands its agents (SPEC §5.6)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from baton import INTENT_INSTRUCTIONS, AsyncClient, Client, Principal
from baton.events import Event
from baton.integrations._llm_text import build_server_instructions
from baton.sinks import FileSink, Sink

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_weather",
        "description": "Weather for a city.",
        "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
    {
        "name": "list_cities",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
SERVER_INFO = {"name": "sunny", "version": "1.4"}


class _CollectingSink(Sink):
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.fail_next = 0

    async def write(self, event: Event) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("collector down")
        self.events.append(event)

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


def _client(sink: _CollectingSink) -> Client:
    return Client(vendor_id="srv-1", tenant_id="ten_abc", sink=sink)


def _snapshots(sink: _CollectingSink) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in sink.events if e.event_type == "surface_snapshot"]


async def _adapter_listed_tool(tmp_path: Path) -> dict[str, Any]:
    from fastmcp import Client as FastMCPClient
    from fastmcp import FastMCP

    from baton import VendorConfig, install_baton

    mcp = FastMCP("parity")

    @mcp.tool()
    def get_weather(city: str) -> str:
        return city

    install_baton(
        mcp,
        VendorConfig(
            vendor_id="srv-1",
            vendor_display_name="Sunny",
            sink=FileSink(str(tmp_path / "events.jsonl")),
        ),
    )
    async with FastMCPClient(mcp) as client:
        listed = next(t for t in await client.list_tools() if t.name == "get_weather")
    return dict(listed.inputSchema)


async def test_user_goal_is_added_exactly_as_an_mcp_adapter_adds_it(tmp_path: Path) -> None:
    adapter = await _adapter_listed_tool(tmp_path)
    client = _client(_CollectingSink())
    served = client.list_tools(TOOLS)
    client.close()

    schema = served[0]["inputSchema"]
    assert schema["properties"]["user_goal"] == adapter["properties"]["user_goal"]
    assert schema["properties"]["user_goal"]["description"].startswith("REQUIRED. ")
    assert "user_goal" in schema["required"]
    assert "user_goal" in adapter["required"]


def test_intent_instructions_are_the_adapters_subagent_sentence() -> None:
    adapter = build_server_instructions(
        vendor_display_name="Sunny", annotation_tool_name="sunny_annotate"
    )
    assert INTENT_INSTRUCTIONS
    assert INTENT_INSTRUCTIONS == INTENT_INSTRUCTIONS.strip()
    assert adapter.endswith(INTENT_INSTRUCTIONS)
    assert "user_goal" in INTENT_INSTRUCTIONS


def test_a_tool_that_declares_user_goal_keeps_its_own() -> None:
    own = {"type": "string", "description": "the vendor's own field"}
    tools = [{"name": "plan", "inputSchema": {"type": "object", "properties": {"user_goal": own}}}]
    client = _client(_CollectingSink())
    served = client.list_tools(tools)
    client.close()

    assert served[0]["inputSchema"]["properties"]["user_goal"] == own
    assert "required" not in served[0]["inputSchema"]


def test_the_snapshot_is_the_vendors_own_surface() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    client.list_tools(TOOLS, server_info=SERVER_INFO, instructions="Sunny's tools.")
    client.close()

    (snapshot,) = _snapshots(sink)
    payload = snapshot["payload"]
    assert payload["server_info"] == SERVER_INFO
    assert payload["instructions"] == "Sunny's tools."
    assert [t["name"] for t in payload["tools"]] == ["get_weather", "list_cities"]
    assert all("user_goal" not in t["inputSchema"]["properties"] for t in payload["tools"])
    assert payload["seam_augmentations"] == {
        "injected_tools": [],
        "intent_param": {
            "names": ["user_goal"],
            "required_names": ["user_goal"],
            "mode": "required",
        },
        "instructions_suffix": None,
    }


def test_the_same_tools_send_one_snapshot_and_are_not_changed() -> None:
    tools = copy.deepcopy(TOOLS)
    sink = _CollectingSink()
    client = _client(sink)
    first = client.list_tools(tools)
    second = client.list_tools(tools)
    client.list_tools(list(reversed(tools)))
    client.close()

    assert tools == TOOLS
    assert first == second
    assert len(_snapshots(sink)) == 1


def test_a_changed_surface_sends_another_snapshot() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    client.list_tools(TOOLS, server_info=SERVER_INFO)
    client.list_tools(TOOLS, server_info={**SERVER_INFO, "version": "1.5"})
    client.list_tools(TOOLS[:1], server_info={**SERVER_INFO, "version": "1.5"})
    client.close()

    hashes = [s["payload"]["surface_hash"] for s in _snapshots(sink)]
    assert len(hashes) == 3
    assert len(set(hashes)) == 3


def test_the_snapshot_takes_no_session_principal_or_client_from_a_block() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.session(
        session_id="conv-1",
        principal=Principal(principal_id="user-7"),
        headers={"User-Agent": "agent/1.0"},
    ):
        client.list_tools(TOOLS)
    client.close()

    (snapshot,) = _snapshots(sink)
    assert snapshot["session_id"] != "conv-1"
    assert snapshot["session_id_origin"] == "minted"
    assert snapshot.get("principal") is None
    assert snapshot.get("client_observed") is None
    assert snapshot.get("transport_observed") is None


def test_a_failed_write_never_raises_and_is_sent_on_the_next_list() -> None:
    sink = _CollectingSink()
    sink.fail_next = 1
    client = _client(sink)
    served = client.list_tools(TOOLS)
    assert "user_goal" in served[0]["inputSchema"]["properties"]
    assert _snapshots(sink) == []

    client.list_tools(TOOLS)
    client.close()
    assert len(_snapshots(sink)) == 1


def test_a_list_it_cannot_read_is_returned_as_it_came() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    unreadable: Any = object()
    assert client.list_tools(unreadable) is unreadable
    client.close()
    assert sink.events == []


def test_a_disabled_client_serves_the_vendors_tools_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BATON_DISABLED", "1")
    sink = _CollectingSink()
    client = _client(sink)
    assert client.list_tools(TOOLS) is TOOLS
    client.close()
    assert sink.events == []


def test_list_tools_after_close_serves_the_list_and_sends_nothing() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    client.close()
    assert client.list_tools(TOOLS)
    assert sink.events == []


async def test_the_async_client_serves_and_snapshots_the_same_way() -> None:
    sink = _CollectingSink()
    sink.fail_next = 1
    client = AsyncClient(vendor_id="srv-1", tenant_id="ten_abc", sink=sink)
    served = await client.list_tools(TOOLS, server_info=SERVER_INFO)
    await client.list_tools(TOOLS, server_info=SERVER_INFO)
    await client.list_tools(TOOLS, server_info=SERVER_INFO)
    await client.aclose()

    sync_sink = _CollectingSink()
    sync_client = _client(sync_sink)
    sync_served = sync_client.list_tools(TOOLS, server_info=SERVER_INFO)
    sync_client.close()

    assert served == sync_served
    (snapshot,) = _snapshots(sink)
    (sync_snapshot,) = _snapshots(sync_sink)
    assert snapshot["payload"] == sync_snapshot["payload"]


async def test_a_disabled_async_client_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BATON_DISABLED", "1")
    sink = _CollectingSink()
    client = AsyncClient(vendor_id="srv-1", tenant_id="ten_abc", sink=sink)
    assert await client.list_tools(TOOLS) is TOOLS
    await client.aclose()
    assert sink.events == []


def test_a_queued_snapshot_does_not_change_when_the_vendor_edits_its_tools() -> None:
    tools = copy.deepcopy(TOOLS)
    sink = _CollectingSink()
    client = _client(sink)
    client.list_tools(tools)
    tools[0]["inputSchema"]["properties"]["city"]["type"] = "integer"
    client.close()

    (snapshot,) = _snapshots(sink)
    weather = next(t for t in snapshot["payload"]["tools"] if t["name"] == "get_weather")
    assert weather["inputSchema"]["properties"]["city"] == {"type": "string"}


def test_mcp_tool_models_come_back_as_models_with_user_goal() -> None:
    from mcp.types import Tool

    models = [Tool(name="get_weather", inputSchema=TOOLS[0]["inputSchema"])]
    sink = _CollectingSink()
    client = _client(sink)
    served = client.list_tools(models)
    client.close()

    assert isinstance(served[0], Tool)
    assert served[0].name == "get_weather"
    assert "user_goal" in served[0].inputSchema["properties"]
    assert "user_goal" not in models[0].inputSchema["properties"]
    (snapshot,) = _snapshots(sink)
    assert snapshot["payload"]["tools"][0]["inputSchema"] == TOOLS[0]["inputSchema"]


def test_a_queued_snapshot_keeps_the_capabilities_it_was_hashed_with() -> None:
    capabilities: dict[str, Any] = {"tools": {"listChanged": False}}
    sink = _CollectingSink()
    client = _client(sink)
    client.list_tools(TOOLS, capabilities=capabilities)
    capabilities["tools"]["listChanged"] = True
    client.close()

    (snapshot,) = _snapshots(sink)
    assert snapshot["payload"]["capabilities"] == {"tools": {"listChanged": False}}


def test_a_surface_already_sent_builds_no_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import baton.client as client_module

    built: list[str] = []
    real = client_module.snapshot_payload

    def counting(surface: dict[str, Any], digest: str) -> Any:
        built.append(digest)
        return real(surface, digest)

    monkeypatch.setattr(client_module, "snapshot_payload", counting)
    client = _client(_CollectingSink())
    for _ in range(3):
        client.list_tools(TOOLS)
    client.close()
    assert len(built) == 1


def test_a_snapshot_that_cannot_be_built_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    import baton.client as client_module

    real = client_module.snapshot_payload
    calls = {"n": 0}

    def fails_once(surface: dict[str, Any], digest: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("odd value")
        return real(surface, digest)

    monkeypatch.setattr(client_module, "snapshot_payload", fails_once)
    sink = _CollectingSink()
    client = _client(sink)
    client.list_tools(TOOLS)
    client.list_tools(TOOLS)
    client.close()
    assert len(_snapshots(sink)) == 1


def test_the_sent_surfaces_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("baton.integrations._surface.SENT_SURFACES_CAP", 2)
    sink = _CollectingSink()
    client = _client(sink)
    for version in ("1", "2", "3", "1"):
        client.list_tools(TOOLS, server_info={"name": "sunny", "version": version})
    client.close()
    assert len(_snapshots(sink)) == 4


async def test_a_cancelled_write_leaves_the_surface_unsent() -> None:
    import asyncio

    class _CancelOnce(_CollectingSink):
        cancelled = False

        async def write(self, event: Event) -> None:
            if not self.cancelled:
                self.cancelled = True
                raise asyncio.CancelledError
            await super().write(event)

    sink = _CancelOnce()
    client = AsyncClient(vendor_id="srv-1", tenant_id="ten_abc", sink=sink)
    with pytest.raises(asyncio.CancelledError):
        await client.list_tools(TOOLS)
    await client.list_tools(TOOLS)
    await client.aclose()
    assert len(_snapshots(sink)) == 1
