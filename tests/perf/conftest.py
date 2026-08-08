from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.perf.harness import FakeCollector


@pytest.fixture
def collector() -> Iterator[FakeCollector]:
    fc = FakeCollector()
    try:
        yield fc
    finally:
        fc.shutdown()
