"""The vectors are made by ``baton-spec/scripts/generate.py`` driving this SDK, so
running it again shows when this SDK's output has moved away from them."""

from __future__ import annotations

import json
import runpy
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.functional.conftest import SPEC_ROOT

pytestmark = pytest.mark.functional

_GENERATOR = SPEC_ROOT / "scripts" / "generate.py"
# The version the vectors were made with is not part of the wire contract.
_NOT_COMPARED = {"sdk_version"}
# generate.py does not write it; it is the note older producers send.
_NOT_REGENERATED = {"annotation.json"}


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not _GENERATOR.exists():
        pytest.skip(f"baton-spec submodule not checked out ({_GENERATOR} missing)")
    # A copy, so the generator's own paths point into the temp dir and never at
    # the committed vectors.
    root = tmp_path_factory.mktemp("spec")
    (root / "scripts").mkdir()
    script = shutil.copy(_GENERATOR, root / "scripts")
    runpy.run_path(str(script))["write_vectors"]()
    return root / "vectors"


def _comparable(path: Path) -> dict[str, Any]:
    event: dict[str, Any] = json.loads(path.read_text())
    return {k: v for k, v in event.items() if k not in _NOT_COMPARED}


def test_output_matches_every_vector(regenerated: Path) -> None:
    committed = {p.name for p in (SPEC_ROOT / "vectors").glob("*.json")} - _NOT_REGENERATED
    assert {p.name for p in regenerated.glob("*.json")} - _NOT_REGENERATED == committed
    drifted = [
        name
        for name in sorted(committed)
        if _comparable(regenerated / name) != _comparable(SPEC_ROOT / "vectors" / name)
    ]
    assert not drifted, (
        f"output differs from {drifted}; if the change is meant, regenerate the "
        "vectors in baton-spec with scripts/generate.py"
    )
