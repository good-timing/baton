"""Shared fixtures for the functional suite.

``SPEC_ROOT`` and ``event_schema`` were a private copy in every test that
validates against the published wire schema. The submodule path and the
not-checked-out skip policy are one fact, so they get one home — a move of the
submodule or a change to the skip rule is now a single edit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

SPEC_ROOT = Path(__file__).resolve().parents[2] / "baton-spec"


@pytest.fixture(scope="module")
def event_schema() -> dict[str, Any]:
    """The published ``events.schema.json``, or a skip when the submodule is not
    checked out — which is a legitimate state for a fresh clone, not a failure."""
    schema_path = SPEC_ROOT / "events.schema.json"
    if not schema_path.exists():
        pytest.skip(f"baton-spec submodule not checked out ({schema_path} missing)")
    result: dict[str, Any] = json.loads(schema_path.read_text())
    return result
