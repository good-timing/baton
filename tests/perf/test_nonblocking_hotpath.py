"""Crown-jewel test: the SDK must never block or crash the vendor's own
tool calls, regardless of collector health (AGENTS.md: "must NOT block the
vendor's hot path on Console availability"). Exercises all three capture
paths (mcp adapter, fastmcp adapter, sync library ``Client``) against
several broken-collector states.

**Assertion shape: relative ratio, not an absolute ms budget.** Shared CI
runners are noisy; a fixed "p99 < 50ms" gate flakes for reasons unrelated to
the SDK. The primary assertion is the ratio of hot-path latency against a
broken collector to hot-path latency against a healthy one, measured
back-to-back in the same test. A loose absolute backstop (seconds-scale)
additionally catches a true hang without gating on everyday speed.

**Known trap.** Tearing down (``handle.aclose()`` / ``sink.aclose()`` /
``client.close()``) against a dead/slow/erroring collector can itself hang:
``HttpSink.aclose()`` calls ``await self.flush()`` directly, and that await
is NOT bounded by ``shutdown_flush_timeout_seconds`` — that kwarg only
guards the atexit fallback path for a process that exits without ever
calling ``aclose()`` (see ``HttpSink._atexit_flush``). Every sink built here
uses ``harness.TIGHT_SINK_KWARGS`` (zero retries, short timeouts) so a real
``flush()``/``aclose()`` is fast by construction, and every async path
additionally routes teardown through ``harness.bounded_aclose`` as defense
in depth. Don't remove either assuming the atexit kwarg already covers it.

**Marker split within this file.** ``TestLatencyStaysFlatAsyncPaths`` /
``TestLatencyStaysFlatLibraryPath`` measure wall-clock ratios — noisy on
shared CI runners by nature, marked ``perf`` (continue-on-error job, not
merge-blocking). ``TestFailOpenOnBufferOverflow`` only asserts
``result is not None`` — no timing involved, nothing flaky about it — so
it's marked ``functional`` instead and runs in the regular merge-blocking
test job. Don't fold it back under a single module-level ``perf`` marker;
that previously let a real regression to this class hide behind a
continue-on-error job (caught in review — see git history).
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime

import pytest

from baton.events import ToolCallStartEvent, ToolCallStartPayload
from baton.sinks import HttpSink
from tests.perf.harness import (
    TIGHT_SINK_KWARGS,
    FakeCollector,
    dead_url,
    fastmcp_session,
    library_session,
    mcp_session,
    median,
    timed_async_calls,
    timed_calls,
)

N_REPS = 20
RATIO_CEILING = 3.0
ABS_BACKSTOP_SECONDS = 2.0
# Floor under the ratio check's denominator — see _assert_ratio_ok.
MIN_HEALTHY_FLOOR_SECONDS = 0.005

ASYNC_PATHS = {"mcp": mcp_session, "fastmcp": fastmcp_session}


def _make_start_event(sequence_number: int = 1) -> ToolCallStartEvent:
    return ToolCallStartEvent(
        tenant_id="harness-vendor",
        vendor_id="harness-vendor",
        session_id="sess_harness",
        sequence_number=sequence_number,
        captured_at=datetime.now(UTC),
        consent_token="ct_harness",
        agent_runtime="test",
        payload=ToolCallStartPayload(tool_name="t"),
    )


async def _median_latency_async(session_cm: object, url: str, n: int = N_REPS) -> float:
    sink = HttpSink(url=url, api_key="k", **TIGHT_SINK_KWARGS)
    async with session_cm(sink) as call:  # type: ignore[operator]
        samples = await timed_async_calls(call, n, warmup=1)
    return median(samples)


def _median_latency_sync(url: str, n: int = N_REPS) -> float:
    sink = HttpSink(url=url, api_key="k", **TIGHT_SINK_KWARGS)
    with library_session(sink) as call:
        samples = timed_calls(call, n, warmup=1)
    return median(samples)


def _assert_ratio_ok(path_name: str, mode: str, healthy: float, broken: float) -> None:
    # Two INDEPENDENT assertions, not one combined via max(): a max() here
    # would let the loose absolute backstop swallow the ratio check whenever
    # the healthy baseline is near-zero (verified empirically — an earlier
    # version of this test used max() and passed even when write() was
    # patched to await the drain inline, because the resulting ~0.2-0.4s
    # per-call slowdown stayed under the multi-second hang-catcher backstop
    # while the ratio check was never independently enforced). MIN_HEALTHY_
    # FLOOR_SECONDS puts a floor under the ratio's denominator instead, so a
    # near-zero healthy baseline doesn't make the ratio check meaningless —
    # the abs backstop's only job is catching a true multi-second hang.
    ratio_ceiling = max(healthy, MIN_HEALTHY_FLOOR_SECONDS) * RATIO_CEILING
    assert broken < ratio_ceiling, (
        f"{path_name}: {mode}-collector median {broken:.4f}s vs healthy "
        f"{healthy:.4f}s median exceeds the {RATIO_CEILING}x ratio ceiling "
        f"({ratio_ceiling:.4f}s) — the hot path is waiting on the collector."
    )
    assert broken < ABS_BACKSTOP_SECONDS, (
        f"{path_name}: {mode}-collector median {broken:.3f}s exceeds the "
        f"{ABS_BACKSTOP_SECONDS}s absolute hang backstop."
    )


# =============================================================================
# Latency stays flat regardless of collector health
# =============================================================================


class TestLatencyStaysFlatAsyncPaths:
    """mcp + fastmcp adapters."""

    # Wall-clock-timing-based — noisy on shared CI runners by nature, so
    # `perf` (continue-on-error), not `functional` (merge-blocking). See
    # the module docstring's marker-split note.
    pytestmark = pytest.mark.perf

    @pytest.mark.parametrize("path_name", sorted(ASYNC_PATHS))
    async def test_slow_collector(self, path_name: str, collector: FakeCollector) -> None:
        session_cm = ASYNC_PATHS[path_name]
        collector.mode = "healthy"
        healthy = await _median_latency_async(session_cm, collector.url)
        collector.mode = "slow"
        collector.slow_seconds = 3.0
        broken = await _median_latency_async(session_cm, collector.url)
        _assert_ratio_ok(path_name, "slow", healthy, broken)

    @pytest.mark.parametrize("path_name", sorted(ASYNC_PATHS))
    async def test_erroring_collector(self, path_name: str, collector: FakeCollector) -> None:
        session_cm = ASYNC_PATHS[path_name]
        collector.mode = "healthy"
        healthy = await _median_latency_async(session_cm, collector.url)
        collector.mode = "erroring"
        broken = await _median_latency_async(session_cm, collector.url)
        _assert_ratio_ok(path_name, "erroring", healthy, broken)

    @pytest.mark.parametrize("path_name", sorted(ASYNC_PATHS))
    async def test_dead_collector(self, path_name: str, collector: FakeCollector) -> None:
        session_cm = ASYNC_PATHS[path_name]
        collector.mode = "healthy"
        healthy = await _median_latency_async(session_cm, collector.url)
        broken = await _median_latency_async(session_cm, dead_url())
        _assert_ratio_ok(path_name, "dead", healthy, broken)


class TestLatencyStaysFlatLibraryPath:
    """Sync library ``Client`` — same shape, driven through the
    background-thread bridge."""

    pytestmark = pytest.mark.perf

    def test_slow_collector(self, collector: FakeCollector) -> None:
        collector.mode = "healthy"
        healthy = _median_latency_sync(collector.url)
        collector.mode = "slow"
        collector.slow_seconds = 3.0
        broken = _median_latency_sync(collector.url)
        _assert_ratio_ok("library", "slow", healthy, broken)

    def test_erroring_collector(self, collector: FakeCollector) -> None:
        collector.mode = "healthy"
        healthy = _median_latency_sync(collector.url)
        collector.mode = "erroring"
        broken = _median_latency_sync(collector.url)
        _assert_ratio_ok("library", "erroring", healthy, broken)

    def test_dead_collector(self, collector: FakeCollector) -> None:
        collector.mode = "healthy"
        healthy = _median_latency_sync(collector.url)
        broken = _median_latency_sync(dead_url())
        _assert_ratio_ok("library", "dead", healthy, broken)


# =============================================================================
# Fail-open correctness: a full buffer + a promoted overflow warning must
# still let the tool call return its real result. Sharper regression
# catcher than the latency ratio above — this directly targets a regression
# where safe_write's `except Exception` swallow gets removed or narrowed.
#
# Deterministic (asserts `result is not None`, no wall-clock measurement),
# so `functional` (merge-blocking), not `perf` — unlike the two classes
# above, there's no reason to let this one ride as advisory-only.
# =============================================================================


class TestFailOpenOnBufferOverflow:
    pytestmark = pytest.mark.functional

    @pytest.mark.parametrize("path_name", sorted(ASYNC_PATHS))
    async def test_async_paths_survive_promoted_overflow_warning(
        self, path_name: str, collector: FakeCollector
    ) -> None:
        session_cm = ASYNC_PATHS[path_name]
        collector.mode = "erroring"  # nothing drains, buffer stays full
        sink = HttpSink(url=collector.url, api_key="k", buffer_size=1, **TIGHT_SINK_KWARGS)
        sink._enqueue_for_test(_make_start_event())  # pre-fill to capacity
        async with session_cm(sink) as call:  # type: ignore[operator]
            with warnings.catch_warnings():
                warnings.filterwarnings("error", category=UserWarning)
                result = await call()
        assert result is not None

    def test_library_path_survives_promoted_overflow_warning(
        self, collector: FakeCollector
    ) -> None:
        collector.mode = "erroring"
        sink = HttpSink(url=collector.url, api_key="k", buffer_size=1, **TIGHT_SINK_KWARGS)
        sink._enqueue_for_test(_make_start_event())
        with library_session(sink) as call:
            with warnings.catch_warnings():
                warnings.filterwarnings("error", category=UserWarning)
                call()  # must not raise
