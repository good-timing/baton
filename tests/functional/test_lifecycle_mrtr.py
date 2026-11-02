"""A prompt get or resource read that pauses for the client's input is one
request (SPEC §11.4.4), as a paused tool call is one call."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from baton.integrations._mrtr import client_can_resume, paused
from baton.sinks import FileSink
from tests._event_helpers import LIFECYCLE_EVENT_TYPES, read_events

pytestmark = pytest.mark.functional

mcp_types = pytest.importorskip("mcp.types")
if not hasattr(mcp_types, "InputRequiredResult"):
    pytest.skip("this mcp predates paused requests (SEP-2322)", allow_module_level=True)

READ_URI = "file:///ask/x"
ANSWERS = {"q1": mcp_types.ElicitResult(action="accept", content={})}


def _ask() -> Any:
    return mcp_types.InputRequiredResult(
        input_requests={
            "q1": mcp_types.ElicitRequest(
                method="elicitation/create",
                params=mcp_types.ElicitRequestFormParams(
                    mode="form",
                    message="Confirm?",
                    requestedSchema={"type": "object", "properties": {}},
                ),
            )
        },
        request_state="asked",
    )


def _register(mcp: Any, context_class: Any, pauses: int) -> None:
    """Each handler pauses ``pauses`` times, then answers."""
    rounds = {"deploy": 0, "ask": 0}

    def answer(handler: str, done: Any) -> Any:
        rounds[handler] += 1
        return _ask() if rounds[handler] <= pauses else done

    async def deploy(ctx: Any) -> Any:
        return answer("deploy", "Deploy now.")

    async def ask(name: str, ctx: Any) -> Any:
        return answer("ask", f"about {name}")

    # Each library finds its context parameter by its annotation, which
    # `from __future__ import annotations` would leave as a string.
    deploy.__annotations__ = {"ctx": context_class, "return": Any}
    ask.__annotations__ = {"name": str, "ctx": context_class, "return": Any}
    mcp.prompt(name="deploy")(deploy)
    mcp.resource("file:///ask/{name}")(ask)


@asynccontextmanager
async def _session(
    adapter: str, events_path: Path, *, older_client: bool = False, pauses: int = 1
) -> AsyncIterator[Any]:
    sink = FileSink(str(events_path))
    if adapter == "official":
        from mcp import Client
        from mcp.server.mcpserver import Context

        from baton.integrations.official import VendorConfig, install_baton
        from baton.integrations.official._compat import MCPServerClass

        mcp = MCPServerClass("mrtr-official")
    else:
        from fastmcp import Client, Context, FastMCP  # type: ignore[assignment]

        from baton.integrations.standalone import (  # type: ignore[assignment]
            VendorConfig,
            install_baton,
        )

        mcp = FastMCP("mrtr-standalone")
    _register(mcp, Context, pauses)
    handle = install_baton(mcp, VendorConfig(vendor_id="mrtr", consent_token="ct_mrtr", sink=sink))
    try:
        if older_client:
            from tests._mcp_session import connected_session

            async with connected_session(mcp) as session:
                assert session.protocol_version < "2026-07-28"
                yield session
        else:
            async with Client(mcp) as client:
                yield client.session
    finally:
        await handle.aclose()


def _lifecycle(events_path: Path) -> list[dict[str, Any]]:
    return [ev for ev in read_events(events_path) if ev["event_type"] in LIFECYCLE_EVENT_TYPES]


async def _get(session: Any, state: str | None) -> Any:
    answers = None if state is None else ANSWERS
    return await session.get_prompt(
        "deploy", input_responses=answers, request_state=state, allow_input_required=True
    )


async def _read(session: Any, state: str | None) -> Any:
    answers = None if state is None else ANSWERS
    return await session.read_resource(
        READ_URI, input_responses=answers, request_state=state, allow_input_required=True
    )


Round = Callable[[Any, "str | None"], Awaitable[Any]]


@pytest.mark.parametrize("adapter", ["official", "standalone"])
@pytest.mark.parametrize(("send", "stem"), [(_get, "prompt_get"), (_read, "resource_read")])
@pytest.mark.parametrize("pauses", [1, 2])
async def test_a_paused_prompt_or_read_is_one_request(
    adapter: str, send: Round, stem: str, pauses: int, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"
    async with _session(adapter, events_path, pauses=pauses) as session:
        result = await send(session, None)
        for _ in range(pauses):
            assert isinstance(result, mcp_types.InputRequiredResult)
            result = await send(session, result.request_state)
        assert not isinstance(result, mcp_types.InputRequiredResult)
    assert [ev["event_type"] for ev in _lifecycle(events_path)] == [f"{stem}_start", f"{stem}_end"]


@pytest.mark.parametrize("adapter", ["official", "standalone"])
async def test_a_pause_the_client_never_answers_has_no_end(adapter: str, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    async with _session(adapter, events_path) as session:
        await _get(session, None)
    assert [ev["event_type"] for ev in _lifecycle(events_path)] == ["prompt_get_start"]


@pytest.mark.parametrize("adapter", ["official", "standalone"])
@pytest.mark.parametrize(("send", "stem"), [(_get, "prompt_get"), (_read, "resource_read")])
async def test_a_pause_an_older_client_cannot_take_is_an_error(
    adapter: str, send: Round, stem: str, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"
    async with _session(adapter, events_path, older_client=True) as session:
        # mcp fails the pause after the seam returns; fastmcp raises inside it.
        with pytest.raises(Exception, match=r"(?i)invalid result|2026-07-28") as refused:
            await send(session, None)
    events = _lifecycle(events_path)
    assert [ev["event_type"] for ev in events] == [f"{stem}_start", f"{stem}_error"]
    assert events[1]["payload"]["error_body"] == str(refused.value)


def test_a_wire_shaped_pause_is_a_pause() -> None:
    assert paused({"resultType": "input_required"})
    assert not paused({"resultType": "complete"})


class _Context:
    def __init__(self, protocol_version: Any) -> None:
        self.protocol_version = protocol_version


def test_only_mcps_own_pausing_protocols_can_resume() -> None:
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    for version in MODERN_PROTOCOL_VERSIONS:
        assert client_can_resume(_Context(version))
    assert not client_can_resume(_Context("2025-11-25"))
    assert not client_can_resume(_Context("DRAFT-2026-v1"))
