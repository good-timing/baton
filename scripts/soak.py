#!/usr/bin/env python
"""Standalone soak/load script for the Baton SDK's ``HttpSink`` hot path.

Not a pytest test — run manually before a PLG launch and periodically
after:

    .venv/bin/python scripts/soak.py --concurrency 20

Drives N concurrent simulated "sessions" against a fake MCP server wrapped
with ``install_baton``, pointed at a fake local collector
(``tests/perf/harness.py`` — reused here so the CI-gated fast tests and
this heavier soak share one implementation), through a healthy -> dead ->
recovered collector-health sequence timed to cross the real (unshortened)
circuit-breaker reset window. Unlike the CI-gated tests in ``tests/perf/``,
this uses ``HttpSink``'s REAL default timings (retry/backoff/circuit
breaker) — the point is to validate the actual defaults a PLG customer runs
with, not a tightened test config.

Advisory only: prints a report and writes an optional JSON file, but does
not exit non-zero or gate anything. Fake collector + fake MCP server only —
no real vendor SDK, no real Console — matching the repo's fake-vendor-only
test boundary rule even though this isn't a pytest test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from baton.sinks import HttpSink  # noqa: E402
from tests.perf.harness import FakeCollector, mcp_session, median, percentile  # noqa: E402

logging.getLogger("baton").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)


@dataclass
class _Sample:
    t: float
    phase: str
    latency_s: float


@dataclass
class _SoakState:
    samples: list[_Sample] = field(default_factory=list)
    buffer_occupancy: list[tuple[float, str, int]] = field(default_factory=list)
    breaker_transitions: list[tuple[float, str, str]] = field(default_factory=list)
    emitted: int = 0


async def _run_session(
    call: Any, state: _SoakState, phase_fn: Any, stop_at: float, rate_seconds: float
) -> None:
    while time.monotonic() < stop_at:
        phase = phase_fn()
        start = time.monotonic()
        try:
            await call()
        except Exception:
            pass
        state.samples.append(_Sample(t=start, phase=phase, latency_s=time.monotonic() - start))
        await asyncio.sleep(rate_seconds)


async def _monitor(sink: HttpSink, state: _SoakState, phase_fn: Any, stop_at: float) -> None:
    last_open: bool | None = None
    while time.monotonic() < stop_at:
        phase = phase_fn()
        occupancy = len(sink._buffer)  # internal — dev tool, mirrors test access patterns
        state.buffer_occupancy.append((time.monotonic(), phase, occupancy))
        is_open = not sink._circuit.can_request()
        if last_open is not None and is_open != last_open:
            state.breaker_transitions.append(
                (time.monotonic(), phase, "opened" if is_open else "closed")
            )
        last_open = is_open
        await asyncio.sleep(0.5)


def _percentiles_by_phase(samples: list[_Sample]) -> dict[str, dict[str, float]]:
    by_phase: dict[str, list[float]] = {}
    for s in samples:
        by_phase.setdefault(s.phase, []).append(s.latency_s)
    return {
        phase: {
            "p50_ms": round(median(lat) * 1000, 3),
            "p95_ms": round(percentile(lat, 95) * 1000, 3),
            "p99_ms": round(percentile(lat, 99) * 1000, 3),
            "count": len(lat),
        }
        for phase, lat in by_phase.items()
    }


def _max_occupancy_by_phase(state: _SoakState) -> dict[str, int]:
    by_phase: dict[str, int] = {}
    for _, phase, occ in state.buffer_occupancy:
        by_phase[phase] = max(by_phase.get(phase, 0), occ)
    return by_phase


async def run_soak(
    *,
    concurrency: int,
    healthy_seconds: float,
    dead_seconds: float,
    recover_seconds: float,
    rate_seconds: float,
    report_path: str | None,
) -> None:
    start = time.monotonic()
    total_seconds = healthy_seconds + dead_seconds + recover_seconds
    healthy_end = start + healthy_seconds
    dead_end = healthy_end + dead_seconds
    stop_at = start + total_seconds

    def phase_fn() -> str:
        now = time.monotonic()
        if now < healthy_end:
            return "healthy"
        if now < dead_end:
            return "dead"
        return "recovered"

    collector = FakeCollector()
    state = _SoakState()
    sink = HttpSink(url=collector.url, api_key="soak-key")  # REAL default kwargs — see module docstring

    # Count every enqueue attempt (start/end/annotation events) so the report
    # can estimate drops: emitted - received-by-collector - still-buffered.
    original_enqueue = sink._enqueue

    def _counting_enqueue(event: Any) -> None:
        state.emitted += 1
        original_enqueue(event)

    sink._enqueue = _counting_enqueue  # type: ignore[method-assign]

    print(
        f"soak: concurrency={concurrency} healthy={healthy_seconds:.0f}s "
        f"dead={dead_seconds:.0f}s recovered={recover_seconds:.0f}s "
        f"(total {total_seconds:.0f}s)"
    )
    if dead_seconds <= 30.0:
        print(
            "soak: WARNING --dead-seconds is <= HttpSink's default "
            "circuit_breaker_reset_seconds (30s) — the breaker may never "
            "reach a real open->half-open transition this run."
        )

    async with mcp_session(sink) as call:

        async def health_switcher() -> None:
            while time.monotonic() < stop_at:
                collector.mode = "healthy" if phase_fn() != "dead" else "erroring"
                await asyncio.sleep(0.5)

        sessions = [
            _run_session(call, state, phase_fn, stop_at, rate_seconds) for _ in range(concurrency)
        ]
        await asyncio.gather(health_switcher(), _monitor(sink, state, phase_fn, stop_at), *sessions)

    collector.shutdown()

    dropped_estimate = max(0, state.emitted - len(collector.received) - len(sink._buffer))
    report = {
        "concurrency": concurrency,
        "total_seconds": total_seconds,
        "total_calls": len(state.samples),
        "throughput_calls_per_sec": round(len(state.samples) / total_seconds, 2)
        if total_seconds
        else 0,
        "latency_by_phase": _percentiles_by_phase(state.samples),
        "buffer_occupancy_max_by_phase": _max_occupancy_by_phase(state),
        "breaker_transitions": [
            {"t_offset_s": round(t - start, 2), "phase": phase, "event": event}
            for t, phase, event in state.breaker_transitions
        ],
        "events_emitted": state.emitted,
        "events_received_by_collector": len(collector.received),
        "events_still_buffered_at_end": len(sink._buffer),
        "events_dropped_estimate": dropped_estimate,
    }

    print(json.dumps(report, indent=2))
    if report_path:
        Path(report_path).write_text(json.dumps(report, indent=2))
        print(f"soak: report written to {report_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--concurrency", type=int, default=10, help="Simulated concurrent sessions.")
    parser.add_argument("--healthy-seconds", type=float, default=10.0)
    parser.add_argument(
        "--dead-seconds",
        type=float,
        default=35.0,
        help="Should exceed HttpSink's default circuit_breaker_reset_seconds "
        "(30s) to exercise a real open->half-open transition.",
    )
    parser.add_argument("--recover-seconds", type=float, default=15.0)
    parser.add_argument(
        "--rate-seconds", type=float, default=0.2, help="Delay between calls per simulated session."
    )
    parser.add_argument("--report", type=str, default=None, help="Path to write a JSON report.")
    args = parser.parse_args()

    asyncio.run(
        run_soak(
            concurrency=args.concurrency,
            healthy_seconds=args.healthy_seconds,
            dead_seconds=args.dead_seconds,
            recover_seconds=args.recover_seconds,
            rate_seconds=args.rate_seconds,
            report_path=args.report,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
