"""The renamed adapter packages keep working at their old import paths.

``baton.integrations.mcp`` -> ``baton.integrations.official`` and
``baton.integrations.fastmcp`` -> ``baton.integrations.standalone``. The
aliases exist for exactly one release and then get deleted; these tests are
what make that deletion deliberate rather than incidental, and they pin the
two properties the deletion plan depends on — that the old path resolves to
the SAME objects (so a caller cannot be half-migrated), and that it resolves
SILENTLY (the decision was no DeprecationWarning, because with no customers
the only reader would be us).
"""

from __future__ import annotations

import importlib
import sys
import warnings
from types import ModuleType

import pytest

ALIASES = [
    ("baton.integrations.mcp", "baton.integrations.official"),
    ("baton.integrations.fastmcp", "baton.integrations.standalone"),
]

PUBLIC_NAMES = [
    "BatonHandle",
    "SessionResolutionContext",
    "VendorConfig",
    "install_baton",
]


def _fresh_import(name: str) -> ModuleType:
    """Import ``name`` with its cache entry dropped, so import-time side
    effects (a warning, say) actually run instead of being skipped because an
    earlier test already imported it."""
    sys.modules.pop(name, None)
    return importlib.import_module(name)


@pytest.mark.parametrize(("old", "new"), ALIASES)
def test_old_path_exports_the_same_objects(old: str, new: str) -> None:
    old_mod = importlib.import_module(old)
    new_mod = importlib.import_module(new)
    for attr in PUBLIC_NAMES:
        assert getattr(old_mod, attr) is getattr(new_mod, attr), (
            f"{old}.{attr} is not {new}.{attr} — an alias that re-implements "
            f"instead of re-exporting lets the two drift"
        )


@pytest.mark.parametrize(("old", "new"), ALIASES)
def test_old_path_is_silent(old: str, new: str) -> None:
    """No DeprecationWarning. This is a decision, not an omission: see the
    alias module docstrings."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _fresh_import(old)
    assert not [w for w in caught if issubclass(w.category, DeprecationWarning)], (
        "the aliases were chosen to be silent; adding a warning is a change of "
        "posture that should update the alias docstrings and the CHANGELOG too"
    )


@pytest.mark.parametrize(("old", "new"), ALIASES)
def test_old_path_exports_exactly_the_package_api(old: str, new: str) -> None:
    """``__all__`` matches on both sides — a name added to the package but not
    the alias would make the old path quietly poorer than the new one."""
    old_mod = importlib.import_module(old)
    new_mod = importlib.import_module(new)
    assert sorted(old_mod.__all__) == sorted(new_mod.__all__) == sorted(PUBLIC_NAMES)
