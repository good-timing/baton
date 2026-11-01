"""``baton-spec/scrub/cases.json`` must match what this ``Scrubber`` returns today.

The scrub rules themselves are guarded by ``test_scrub.py``. For a change you
meant, regenerate (``.venv/bin/python baton-spec/scripts/generate.py``), commit
``cases.json`` in baton-spec, and move the submodule pin here.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

import pytest

from baton.scrub import DEPTH_MARKER, Scrubber
from tests.functional.conftest import SPEC_ROOT

CASES_PATH = SPEC_ROOT / "scrub" / "cases.json"


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    if not (SPEC_ROOT / "events.schema.json").exists():
        pytest.skip(f"baton-spec submodule not checked out ({SPEC_ROOT})")
    assert CASES_PATH.exists(), f"{CASES_PATH} missing: the baton-spec pin is too old; bump it"
    loaded: list[dict[str, Any]] = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert loaded
    return loaded


def _as_json(value: Any) -> str:
    # `==` treats `True` and `1` as equal; in JSON they differ.
    return json.dumps(value, sort_keys=True)


def test_every_case_scrubs_as_recorded(cases: list[dict[str, Any]]) -> None:
    mismatches = []
    for index, case in enumerate(cases):
        scrubber = Scrubber()
        output = _as_json(scrubber(case["input"]))
        counts = dict(scrubber.counts)
        if output != _as_json(case["output"]) or counts != case["counts"]:
            mismatches.append(
                f"#{index}: expected {_as_json(case['output'])} {case['counts']}, got {output} {counts}"
            )
    assert not mismatches, f"{len(mismatches)} of {len(cases)} cases fail; first 5:\n" + "\n".join(
        mismatches[:5]
    )


def test_one_scrubber_adds_up_counts_and_warns_about_depth_once(
    cases: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    expected: Counter[str] = Counter()
    for case in cases:
        expected.update(case["counts"])
    assert expected["depth_limit"] > 0

    scrubber = Scrubber()
    with caplog.at_level(logging.WARNING, logger="baton.scrub"):
        for case in cases:
            scrubber(case["input"])

    assert scrubber.counts == expected
    assert len([r for r in caplog.records if DEPTH_MARKER in r.getMessage()]) == 1
