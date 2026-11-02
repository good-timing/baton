"""Helpers for the runners of ``baton-spec``'s fake-collector scenario files.
The ``collector`` fixture itself is in ``tests/conftest.py``."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Any

import pytest

from baton.integrations.official import BatonHandle, VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.sinks import HttpSink
from tests.functional.conftest import SPEC_ROOT

COLLECTOR_DIR = SPEC_ROOT / "collector"


def scenario_file(name: str) -> dict[str, Any]:
    """The file's contents, or no scenarios when the submodule is not checked out."""
    path = COLLECTOR_DIR / name
    return json.loads(path.read_text()) if path.exists() else {"scenarios": []}


def each_scenario(
    spec: dict[str, Any], *, not_run: dict[str, str] | None = None, mode: str | None = None
) -> pytest.MarkDecorator:
    """Parametrizes ``scenario`` and the ``collector`` fixture over ``spec``'s
    scenarios. The collector runs in each scenario's mode, or in ``mode``."""
    not_run = not_run or {}
    return pytest.mark.parametrize(
        ("scenario", "collector"),
        [
            pytest.param(
                s,
                {"mode": mode} if mode else s,
                id=s["name"],
                marks=[pytest.mark.skip(reason=not_run[s["name"]])] if s["name"] in not_run else [],
            )
            for s in spec["scenarios"]
        ],
        indirect=["collector"],
    )


def _received(port: int, mode: str, query: str) -> dict[str, Any]:
    if mode == "down":
        return {"accepted": [], "events": [], "requests": 0, "reports": 0}
    url = f"http://127.0.0.1:{port}/_received{query}"
    with urllib.request.urlopen(url, timeout=5) as response:
        received: dict[str, Any] = json.load(response)
        return received


async def received(port: int, mode: str = "ok", query: str = "") -> dict[str, Any]:
    """What the collector has, read off the loop so the SDK keeps sending meanwhile."""
    return await asyncio.to_thread(_received, port, mode, query)


def http_sink(port: int, settings: dict[str, Any] | None = None) -> HttpSink:
    """An ``HttpSink`` to the collector, with a scenario file's ``settings``."""
    if settings is None:
        return HttpSink(url=f"http://127.0.0.1:{port}", api_key="k")
    return HttpSink(
        url=f"http://127.0.0.1:{port}",
        api_key="k",
        request_timeout_seconds=settings["request_timeout_ms"] / 1000,
        shutdown_flush_timeout_seconds=settings["shutdown_budget_ms"] / 1000,
        backoff_base_seconds=settings["backoff_base_ms"] / 1000,
    )


def install_echo(sink: HttpSink, **server: Any) -> tuple[Any, BatonHandle]:
    """The scenario files' server: one tool, ``echo``, with Baton installed."""
    mcp = FastMCP("scenarios", **server)

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    handle = install_baton(mcp, VendorConfig(vendor_id="v", vendor_display_name="V", sink=sink))
    return mcp, handle


def from_baton(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.name == "baton" or r.name.startswith("baton.")]
