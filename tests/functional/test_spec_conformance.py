"""Every event actually emitted by the mcp adapter validates against the
shared wire schema in the ``baton-spec`` submodule (SPEC §11.4) — the
cross-repo counterpart to ``test_cross_path_envelope.py``'s intra-repo
shape checks. This is what would have caught the SPEC §13 `name`/`names`
divergence between the SDK and baton-proxy before it shipped: any producer
whose test suite runs this same check against the same submodule fails
loudly on drift instead of Console silently absorbing both shapes.

Only the mcp adapter is exercised here (not fastmcp/library) since
``test_cross_path_envelope.py`` already proves all three paths emit an
identical shape — one path is enough to prove that shape matches the
shared schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from tests.functional.test_cross_path_envelope import _read_events, _run_mcp_path

pytestmark = pytest.mark.functional

SPEC_ROOT = Path(__file__).resolve().parents[2] / "baton-spec"


@pytest.fixture(scope="module")
def event_schema() -> dict:
    schema_path = SPEC_ROOT / "events.schema.json"
    if not schema_path.exists():
        pytest.skip(f"baton-spec submodule not checked out ({schema_path} missing)")
    return json.loads(schema_path.read_text())


async def test_mcp_path_events_conform_to_shared_schema(event_schema: dict, tmp_path: Path) -> None:
    events_path = str(tmp_path / "events.jsonl")
    await _run_mcp_path(events_path)
    events = _read_events(events_path)

    for event in events:
        jsonschema.validate(event, event_schema)


def test_vectors_still_conform_to_the_schema_shipped_alongside_them(event_schema: dict) -> None:
    vectors_dir = SPEC_ROOT / "vectors"
    vectors = sorted(vectors_dir.glob("*.json"))
    assert vectors, f"no vectors found in {vectors_dir}"

    for vector_path in vectors:
        event = json.loads(vector_path.read_text())
        jsonschema.validate(event, event_schema)
