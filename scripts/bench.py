#!/usr/bin/env python
"""Runs baton-spec's bench/bench.json against the installed baton-sdk and
writes one result file.

    python scripts/bench.py --source published|checkout --out RESULT.json

Only the public API is used, so the same script measures the last release
and the checkout. `make bench` runs both and prints the comparison.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import resource
import subprocess
import sys
import time
import tracemalloc
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from baton.integrations.official import VendorConfig, install_baton
from baton.sinks import HttpSink

try:
    from mcp.server.fastmcp import FastMCP as Server
except ImportError:
    from mcp.server.mcpserver import MCPServer as Server

SPEC = Path(__file__).resolve().parent.parent / "baton-spec"
BENCH = json.loads((SPEC / "bench" / "bench.json").read_text())
TOOL_EVENTS = ("tool_call_start", "tool_call_end")


@contextmanager
def collector(mode: str) -> Iterator[int]:
    if mode == "down":
        yield 1
        return
    process = subprocess.Popen(
        [sys.executable, str(SPEC / "collector" / "fake_collector.py"), "--mode", mode],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        yield int(process.stdout.readline())
    finally:
        process.kill()
        process.wait()


def delivered(port: int, mode: str) -> int:
    if mode == "down":
        return 0
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/_received?lists=0", timeout=10
    ) as response:
        counts = json.load(response)["counts"]
    return sum(counts.get(t, 0) for t in TOOL_EVENTS)


def echo_server(port: int | None) -> tuple[Any, Any]:
    """The bench server, with Baton sending to ``port`` unless it is None."""
    server = Server("bench")

    @server.tool()
    def echo(text: str) -> str:
        return text

    if port is None:
        return server, None
    sink = HttpSink(url=f"http://127.0.0.1:{port}", api_key="k")
    return server, install_baton(
        server, VendorConfig(vendor_id="v", vendor_display_name="V", sink=sink)
    )


@asynccontextmanager
async def client(server: Any) -> AsyncIterator[ClientSession]:
    lowlevel = getattr(server, "_lowlevel_server", None) or server._mcp_server
    async with create_client_server_memory_streams() as (
        (client_read, client_write),
        (server_read, server_write),
    ):
        async with anyio.create_task_group() as tasks:
            options = lowlevel.create_initialization_options()
            tasks.start_soon(
                lambda: lowlevel.run(server_read, server_write, options, raise_exceptions=False)
            )
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()
                yield session
            tasks.cancel_scope.cancel()


async def timed_calls(session: ClientSession, calls: int, concurrency: int) -> list[float]:
    """Each call's seconds, with ``concurrency`` calls in flight at once."""
    times: list[float] = []
    left = iter(range(calls))

    async def worker() -> None:
        for _ in left:
            started = time.perf_counter()
            result = await session.call_tool("echo", {"text": "hello"})
            times.append(time.perf_counter() - started)
            if getattr(result, "is_error", None) or getattr(result, "isError", None):
                raise RuntimeError(f"echo failed: {result.content}")

    async with anyio.create_task_group() as tasks:
        for _ in range(concurrency):
            tasks.start_soon(worker)
    return times


def ms(seconds: float) -> float:
    return seconds * 1000


def pct(samples: list[float], p: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(p / 100 * len(ordered)))]


async def overhead(m: dict[str, Any]) -> dict[str, float]:
    figures: dict[str, list[float]] = {}
    with collector(m["collector"]) as port:
        for name, to in (("plain", None), ("baton", port)):
            server, handle = echo_server(to)
            async with client(server) as session:
                await timed_calls(session, m["warmup"], 1)
                figures[name] = await timed_calls(session, m["calls"], m["concurrency"])
            if handle is not None:
                await handle.aclose()
    out = {f"{n}_p{p}_ms": ms(pct(figures[n], p)) for n in ("plain", "baton") for p in (50, 99)}
    for p in (50, 99):
        out[f"added_p{p}_ms"] = out[f"baton_p{p}_ms"] - out[f"plain_p{p}_ms"]
    return out


async def load(m: dict[str, Any]) -> dict[str, float]:
    with collector(m["collector"]) as port:
        server, handle = echo_server(port)
        async with client(server) as session:
            await timed_calls(session, m["warmup"], 1)
            started = time.perf_counter()
            times = await timed_calls(session, m["calls"], m["concurrency"])
            wall = time.perf_counter() - started
        started = time.perf_counter()
        await handle.aclose()
        closing = time.perf_counter() - started
        emitted = 2 * (m["warmup"] + m["calls"])
        got = delivered(port, m["collector"])
    return {
        "wall_ms": ms(wall),
        "call_p50_ms": ms(pct(times, 50)),
        "call_p99_ms": ms(pct(times, 99)),
        "close_ms": ms(closing),
        "emitted": emitted,
        "delivered": got,
        "lost": emitted - got,
    }


def live_heap() -> int:
    gc.collect()
    return tracemalloc.get_traced_memory()[0]


async def memory(m: dict[str, Any]) -> dict[str, float]:
    half = m["calls"] // 2
    tracemalloc.start()
    try:
        with collector(m["collector"]) as port:
            server, handle = echo_server(port)
            async with client(server) as session:
                await timed_calls(session, m["warmup"], 1)
                heaps = [live_heap()]
                for _ in range(2):
                    await timed_calls(session, half, m["concurrency"])
                    heaps.append(live_heap())
            await handle.aclose()
    finally:
        tracemalloc.stop()
    return {
        "heap_after_warmup_bytes": heaps[0],
        "heap_at_half_bytes": heaps[1],
        "heap_at_end_bytes": heaps[2],
        "growth_per_call_bytes": (heaps[2] - heaps[1]) / half,
    }


RUNNERS = {
    "overhead": overhead,
    "burst": load,
    "outage_hang": load,
    "outage_down": load,
    "memory": memory,
    "memory_outage": memory,
}


async def run() -> tuple[dict[str, dict[str, float]], int]:
    """Each measure's metrics, and the peak memory before the first memory measure."""
    measures: dict[str, dict[str, float]] = {}
    peak = 0
    for m in BENCH["measures"]:
        if m["name"].startswith("memory") and not peak:
            peak = max_rss_bytes()
        measures[m["name"]] = await RUNNERS[m["name"]](m)
    return measures, peak or max_rss_bytes()


def max_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("published", "checkout"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    measures, peak = anyio.run(run)
    result = {
        "sdk": "python",
        "version": version("baton-sdk"),
        "source": args.source,
        "runtime": f"{platform.python_implementation()} {platform.python_version()}",
        "max_rss_bytes": peak,
        "measures": measures,
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
