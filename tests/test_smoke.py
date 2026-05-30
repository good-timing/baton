"""Smoke test — verifies the package imports and exposes its version markers.

Real test suite arrives with Week 1's event-emitter implementation; this exists
so ``make test`` is green from the initial commit.
"""

from __future__ import annotations

import baton


def test_version_present() -> None:
    assert baton.__version__.startswith("0.1.")


def test_spec_version_present() -> None:
    assert baton.SPEC_VERSION == "0.2"
