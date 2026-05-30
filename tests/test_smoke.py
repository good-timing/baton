"""Smoke test — verifies the package imports and exposes its version marker."""

from __future__ import annotations

import baton


def test_version_present() -> None:
    assert baton.__version__.startswith("0.1.")
