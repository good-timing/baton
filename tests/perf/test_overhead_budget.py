"""Per-call overhead budget: measures the SDK's added latency (PII scrubbing
+ serialization + emit) over a plain, uninstrumented tool call, across a
payload matrix designed to exercise ``scrub.py``'s cost surface (recursion
depth, breadth, PII-pattern-dense strings, Luhn-candidate-dense strings).

**Fair baseline.** The *same* handler function, called two ways — (a)
direct on a plain FastMCP server, vs (b) identical fn on a server with
``install_baton`` applied, pointed at a ``NullSink`` (zero I/O — isolates
scrub+serialize CPU cost from network variance; see ``harness.NullSink``).

**Warmup.** The first tool call through an installed server triggers a
one-time ``surface_snapshot`` build+hash+emit (``_tool_wrap.py``'s
``_SurfaceState``) — a real, non-representative cost. Discarded via
``warmup=WARMUP`` on every timing series. The snapshot's own
dedup/correctness is already covered by
``tests/integrations/mcp/test_install.py::TestSurfaceSnapshot`` — not
re-tested here.

**Assertion shape.** Primary gate: a per-payload-class absolute backstop on
``median(wrapped)`` — real work here is sub-millisecond (measured on
dev hardware: 0.02-0.2ms across the whole matrix), so backstops carry ~50x+
headroom over that before tripping, comfortably absorbing noisy-CI-runner
variance while still catching a true pathological regression (e.g.
``scrub.DEPTH_LIMIT`` becoming unbounded, or an accidental O(n^2) walk).
Secondary: ``median(wrapped) / median(baseline)`` — deliberately loose.
The baseline handler does no scrubbing/serialization at all, so it measures
within noise of the timer-resolution floor (~0.005-0.01ms); real scrub work
against ANY non-trivial payload legitimately lands 10-25x above that floor
even in a healthy build (measured), so the ratio is a tripwire for a
multi-order-of-magnitude blowup, not a tight budget — the absolute backstop
is the assertion that actually matters here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from baton.integrations.mcp import VendorConfig, install_baton
from baton.integrations.mcp._compat import MCPServerClass as FastMCP
from tests.perf.harness import NullSink, median, percentile, record_measurement, timed_async_calls

pytestmark = pytest.mark.perf

N_REPS = 30
WARMUP = 3


def _flat_small() -> dict[str, Any]:
    return {"a": "hello", "b": "world", "c": 42, "d": True}


def _nested_deep(depth: int = 8) -> dict[str, Any]:
    node: dict[str, Any] = {"leaf": "value"}
    for i in range(depth):
        node = {f"level_{i}": node}
    return node


def _wide(n: int = 50) -> dict[str, Any]:
    return {f"key_{i}": f"value_{i}" for i in range(n)}


def _pii_heavy(n: int = 20) -> dict[str, Any]:
    sample = (
        "contact jane.doe@example.com or +1 415-555-0199; "
        "bearer token Bearer abcdefghijklmnopqrstuvwxyz0123456789; "
        "api key sk-abcdefghijklmnopqrstuvwxyz0123456789"
    )
    return {f"note_{i}": sample for i in range(n)}


def _luhn_heavy(n: int = 20) -> dict[str, Any]:
    valid = "4111111111111111"  # Luhn-valid test card number
    invalid = "4111111111111112"  # same length, Luhn-invalid
    return {f"num_{i}": (valid if i % 2 == 0 else invalid) for i in range(n)}


# name -> (payload factory, ratio ceiling, absolute backstop seconds).
# Calibrated against measured dev-hardware medians (wrapped: flat_small
# ~0.01ms, nested_deep ~0.02ms, wide ~0.09ms, pii_heavy ~0.16ms,
# luhn_heavy ~0.07ms) with ~50-100x headroom on the absolute backstop and a
# ratio ceiling loose enough to tolerate a near-zero baseline denominator —
# see the module docstring for why the ratio is a tripwire, not the gate.
PAYLOAD_CASES: dict[str, tuple[Callable[[], dict[str, Any]], float, float]] = {
    "flat_small": (_flat_small, 15.0, 0.005),
    "nested_deep": (_nested_deep, 40.0, 0.01),
    "wide": (_wide, 40.0, 0.01),
    "pii_heavy": (_pii_heavy, 40.0, 0.02),
    "luhn_heavy": (_luhn_heavy, 40.0, 0.01),
}


def _plain_server() -> Any:
    mcp = FastMCP("baseline-mcp")

    @mcp.tool()
    def echo(payload: dict[str, Any]) -> dict[str, Any]:
        return {"received": True}

    return mcp


async def _wrapped_server() -> tuple[Any, Any]:
    mcp = FastMCP("wrapped-mcp")

    @mcp.tool()
    def echo(payload: dict[str, Any]) -> dict[str, Any]:
        return {"received": True}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="harness-vendor",
            vendor_display_name="Harness Vendor",
            consent_token="ct_harness",
            sink=NullSink(),
        ),
    )
    return mcp, handle


@pytest.mark.parametrize("case_name", sorted(PAYLOAD_CASES))
async def test_overhead_ratio_within_budget(case_name: str) -> None:
    make_payload, ratio_ceiling, abs_backstop = PAYLOAD_CASES[case_name]
    payload = make_payload()

    baseline_mcp = _plain_server()

    async def call_baseline() -> Any:
        return await baseline_mcp.call_tool("echo", {"payload": payload})

    baseline_samples = await timed_async_calls(call_baseline, N_REPS, warmup=WARMUP)
    baseline_median = median(baseline_samples)

    wrapped_mcp, handle = await _wrapped_server()
    try:

        async def call_wrapped() -> Any:
            return await wrapped_mcp.call_tool("echo", {"payload": payload})

        wrapped_samples = await timed_async_calls(call_wrapped, N_REPS, warmup=WARMUP)
    finally:
        await handle.aclose()  # NullSink — no I/O, aclose() is instant

    wrapped_median = median(wrapped_samples)
    wrapped_p95 = percentile(wrapped_samples, 95)
    # Guard the ratio's denominator: a baseline that measures as ~0s (sub-
    # timer-resolution) would otherwise divide-by-near-zero and blow the
    # ratio ceiling on pure noise.
    ratio = wrapped_median / max(baseline_median, 1e-6)

    # Recorded BEFORE asserting, so the number shows up in the CI step
    # summary even when the assertion below fails.
    record_measurement(
        suite="overhead_budget",
        case=case_name,
        baseline_median_ms=baseline_median * 1000,
        wrapped_median_ms=wrapped_median * 1000,
        wrapped_p95_ms=wrapped_p95 * 1000,
        ratio=ratio,
    )

    assert wrapped_median < abs_backstop, (
        f"{case_name}: wrapped median {wrapped_median * 1000:.2f}ms exceeds the "
        f"{abs_backstop * 1000:.0f}ms absolute backstop (p95={wrapped_p95 * 1000:.2f}ms)."
    )
    assert ratio < ratio_ceiling, (
        f"{case_name}: wrapped/baseline ratio {ratio:.2f}x exceeds the "
        f"{ratio_ceiling}x ceiling (baseline median={baseline_median * 1000:.3f}ms, "
        f"wrapped median={wrapped_median * 1000:.3f}ms, wrapped p95={wrapped_p95 * 1000:.2f}ms)."
    )
