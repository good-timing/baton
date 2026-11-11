#!/usr/bin/env python
"""Runs baton-spec's bench/bench.json against the installed baton-sdk and
writes one result file.

    python scripts/bench.py --source published|checkout --out RESULT.json [--only MEASURE]

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
        [
            sys.executable,
            str(SPEC / "collector" / "fake_collector.py"),
            "--mode",
            mode,
            "--no-events",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        yield int(process.stdout.readline())
    finally:
        process.kill()
        process.wait()


def losses(port: int, m: dict[str, Any]) -> dict[str, float]:
    """Emitted, delivered and lost for ``m``'s calls, TOOL_EVENTS each."""
    emitted = len(TOOL_EVENTS) * (m["warmup"] + m["calls"])
    got = delivered(port, m["collector"])
    return {"emitted": emitted, "delivered": got, "lost": emitted - got}


def delivered(port: int, mode: str) -> int:
    if mode == "down":
        return 0
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/_received?lists=0", timeout=10
    ) as response:
        counts = json.load(response)["counts"]
    return sum(counts.get(t, 0) for t in TOOL_EVENTS)


def echo_server(port: int | None, result_bytes: int = 0) -> tuple[Any, Any]:
    """The bench server, with Baton sending to ``port`` unless it is None,
    and the large tool when ``result_bytes`` is set."""
    server = Server("bench")

    @server.tool()
    def echo(text: str) -> str:
        return text

    if result_bytes:
        large_text = ("hello " * (result_bytes // 6 + 1))[:result_bytes]

        # Without structured_output=False the SDK sends the text a second time.
        @server.tool(structured_output=False)
        def large() -> str:
            return large_text

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


ECHO = ("echo", {"text": "hello"})
LARGE: tuple[str, dict[str, Any]] = ("large", {})


async def timed_calls(
    session: ClientSession, calls: int, concurrency: int, call: tuple[str, dict[str, Any]] = ECHO
) -> list[float]:
    """Each call's seconds, with ``concurrency`` calls in flight at once."""
    times: list[float] = []
    left = iter(range(calls))

    async def worker() -> None:
        for _ in left:
            started = time.perf_counter()
            result = await session.call_tool(*call)
            times.append(time.perf_counter() - started)
            if getattr(result, "is_error", None) or getattr(result, "isError", None):
                raise RuntimeError(f"{call[0]} failed: {result.content}")

    async with anyio.create_task_group() as tasks:
        for _ in range(concurrency):
            tasks.start_soon(worker)
    return times


def ms(seconds: float) -> float:
    return seconds * 1000


def pct(samples: list[float], p: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(p / 100 * len(ordered)))]


async def one_pass(
    m: dict[str, Any], port: int | None, call: tuple[str, dict[str, Any]] = ECHO
) -> tuple[list[float], float, Any]:
    """Call times, wall time and the still-open install handle of one pass on a new server."""
    server, handle = echo_server(port, m.get("result_bytes", 0))
    async with client(server) as session:
        await timed_calls(session, m["warmup"], 1, call)
        started = time.perf_counter()
        times = await timed_calls(session, m["calls"], m["concurrency"], call)
        wall = time.perf_counter() - started
    return times, wall, handle


async def added(m: dict[str, Any], port: int, call: tuple[str, dict[str, Any]]) -> dict[str, float]:
    """The call times on a server without Baton, then on one with it, and
    what Baton adds to them."""
    figures: dict[str, list[float]] = {}
    for name, to in (("plain", None), ("baton", port)):
        figures[name], _, handle = await one_pass(m, to, call)
        if handle is not None:
            await handle.aclose()
    out = {f"{n}_p{p}_ms": ms(pct(figures[n], p)) for n in ("plain", "baton") for p in (50, 99)}
    for p in (50, 99):
        out[f"added_p{p}_ms"] = out[f"baton_p{p}_ms"] - out[f"plain_p{p}_ms"]
    return out


async def overhead(m: dict[str, Any]) -> dict[str, float]:
    with collector(m["collector"]) as port:
        return await added(m, port, ECHO)


async def large_result(m: dict[str, Any]) -> dict[str, float]:
    with collector(m["collector"]) as port:
        out = await added(m, port, LARGE) | losses(port, m)
        # Read on a second Baton server, untimed: tracemalloc slows every
        # allocation, and Baton's side allocates more than the plain one.
        # Its events come after the count above.
        server, handle = echo_server(port, m["result_bytes"])
        async with client(server) as session:
            await timed_calls(session, m["warmup"], 1, LARGE)
            tracemalloc.start()
            try:
                before = live_heap()
                await timed_calls(session, m["calls"], m["concurrency"], LARGE)
                out["heap_growth_bytes"] = live_heap() - before
            finally:
                tracemalloc.stop()
        await handle.aclose()
    return out


async def load(m: dict[str, Any]) -> dict[str, float]:
    with collector(m["collector"]) as port:
        # So that neither pass's times include collecting garbage made before it.
        gc.collect()
        plain, plain_wall, _ = await one_pass(m, None)
        gc.collect()
        times, wall, handle = await one_pass(m, port)
        started = time.perf_counter()
        await handle.aclose()
        out = {"close_ms": ms(time.perf_counter() - started)}
        for prefix, t, w in (("plain_", plain, plain_wall), ("", times, wall)):
            out[f"{prefix}wall_ms"] = ms(w)
            for p in (50, 99):
                out[f"{prefix}call_p{p}_ms"] = ms(pct(t, p))
        for p in (50, 99):
            out[f"added_call_p{p}_ms"] = out[f"call_p{p}_ms"] - out[f"plain_call_p{p}_ms"]
        return losses(port, m) | out


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
    "large_result": large_result,
    "burst": load,
    "outage_hang": load,
    "outage_down": load,
    "memory": memory,
    "memory_outage": memory,
}


async def run(only: str | None) -> tuple[dict[str, dict[str, float]], int]:
    """Each measure's metrics, and the peak memory before the first memory measure
    (at the end when none ran)."""
    measures: dict[str, dict[str, float]] = {}
    peak = 0
    for m in BENCH["measures"]:
        if only not in (None, m["name"]):
            continue
        if m["name"].startswith("memory") and not peak:
            peak = max_rss_bytes()
        measures[m["name"]] = await RUNNERS[m["name"]](m)
    return measures, peak or max_rss_bytes()


def max_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def cpu_model() -> str:
    if sys.platform == "darwin":
        command = ["sysctl", "-n", "machdep.cpu.brand_string"]
        return subprocess.run(command, capture_output=True, text=True).stdout.strip() or "unknown"
    try:
        lines = Path("/proc/cpuinfo").read_text().splitlines()
    except OSError:
        lines = []
    model = next(
        (line.partition(":")[2].strip() for line in lines if line.startswith("model name")), ""
    )
    return model or "unknown"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("published", "checkout"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--only",
        choices=[m["name"] for m in BENCH["measures"]],
        help="run this measure alone",
    )
    args = parser.parse_args()
    measures, peak = anyio.run(run, args.only)
    result = {
        "sdk": "python",
        "version": version("baton-sdk"),
        "source": args.source,
        "runtime": f"{platform.python_implementation()} {platform.python_version()}",
        "cpu": cpu_model(),
        "max_rss_bytes": peak,
        "measures": measures,
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
