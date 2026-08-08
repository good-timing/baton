from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from tests.perf.harness import FakeCollector, collected_measurements


@pytest.fixture
def collector() -> Iterator[FakeCollector]:
    fc = FakeCollector()
    try:
        yield fc
    finally:
        fc.shutdown()


# =============================================================================
# CI step-summary reporting — publishes the numbers behind the perf suite's
# pass/fail so they're visible per-run without extra infra. Single-run
# snapshot, not a historical trend (see README note in the rendered report
# for why: shared CI runners are noisy, and nothing here persists past the
# run). tests/functional/'s conftest doesn't need this hook — those tests
# assert deterministic behavior, not measured numbers.
# =============================================================================


def _render_markdown(measurements: list[dict[str, Any]]) -> str:
    hotpath = [m for m in measurements if m.get("suite") == "nonblocking_hotpath"]
    overhead = [m for m in measurements if m.get("suite") == "overhead_budget"]

    lines = [
        "## Perf test results",
        "",
        "Single-run wall-clock numbers from this CI runner — noisy by nature "
        "(see `tests/perf/test_nonblocking_hotpath.py` / `test_overhead_budget.py` "
        "module docstrings for the assertion methodology). This is a snapshot of "
        "this run, not a historical trend.",
        "",
    ]

    if hotpath:
        lines += [
            "### Non-blocking hot path — broken-collector latency vs. healthy baseline",
            "",
            "| Path | Collector state | Healthy median | Broken median | Ratio |",
            "|---|---|---:|---:|---:|",
        ]
        for m in sorted(hotpath, key=lambda r: (r["path"], r["mode"])):
            lines.append(
                f"| {m['path']} | {m['mode']} | {m['healthy_median_ms']:.3f} ms | "
                f"{m['broken_median_ms']:.3f} ms | {m['ratio']:.2f}x |"
            )
        lines.append("")

    if overhead:
        lines += [
            "### Per-call overhead budget — Baton-wrapped vs. plain baseline",
            "",
            "| Payload | Baseline median | Wrapped median | Wrapped p95 | Ratio |",
            "|---|---:|---:|---:|---:|",
        ]
        for m in sorted(overhead, key=lambda r: r["case"]):
            lines.append(
                f"| {m['case']} | {m['baseline_median_ms']:.4f} ms | "
                f"{m['wrapped_median_ms']:.4f} ms | {m['wrapped_p95_ms']:.4f} ms | "
                f"{m['ratio']:.2f}x |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    measurements = collected_measurements()
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    # Diagnostic — always printed (goes to the step's plain log, visible via
    # `gh run view --log`) so a silent miss (0 measurements, unset env var,
    # write exception) is visible without needing to inspect the rendered
    # summary UI to tell whether this hook even ran.
    print(
        f"\n[perf-summary] {len(measurements)} measurement(s) collected; "
        f"GITHUB_STEP_SUMMARY={'set: ' + summary_path if summary_path else 'unset'}"
    )
    if not measurements:
        return
    report = _render_markdown(measurements)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            written = f.write(report)
        print(f"[perf-summary] wrote {written} chars to {summary_path}")
    else:
        # Local run — no step-summary file to append to. Print instead so
        # the numbers are visible without needing CI.
        print("\n" + report)
