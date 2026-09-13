"""The renamed adapter packages keep working at their old import paths.

``baton.integrations.mcp`` -> ``baton.integrations.official`` and
``baton.integrations.fastmcp`` -> ``baton.integrations.standalone``. The
The aliases are temporary, but NOT on a release count — "one release" was the
original plan and it did not survive contact with the sibling repos; the shim
docstrings carry the real precondition, which is a grep that must come back
empty. These tests make the deletion deliberate rather than incidental, and pin
the
three properties the deletion plan depends on — that the old path resolves to
the SAME objects (so a caller cannot be half-migrated), that it resolves
SILENTLY (the decision was no DeprecationWarning, because with no customers
the only reader would be us), and that SUBMODULE imports work too.

The last one is here because its absence shipped: the first cut of these
shims re-exported only the four top-level names, so
``from baton.integrations.mcp._compat import MCPServerClass`` raised
ModuleNotFoundError while this file was green and the CHANGELOG claimed the
old paths kept working. That exact import is in
``baton-spec/scripts/generate.py``, vendored into three repos.
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


@pytest.mark.parametrize(("old", "new"), ALIASES)
def test_every_submodule_resolves_at_the_old_path(old: str, new: str) -> None:
    """Not a hand-listed set: whatever the renamed package actually contains is
    what the old path must expose, so a submodule added while the shims live is
    covered without anyone remembering this file."""
    import pkgutil

    new_pkg = importlib.import_module(new)
    names = [m.name for m in pkgutil.iter_modules(new_pkg.__path__)]
    assert names, f"{new} exposed no submodules — the discovery is broken, not the package"

    importlib.import_module(old)  # registers the aliases
    for name in names:
        old_sub = importlib.import_module(f"{old}.{name}")
        new_sub = importlib.import_module(f"{new}.{name}")
        assert old_sub is new_sub, (
            f"{old}.{name} is a SECOND module object loaded from the same file as "
            f"{new}.{name}; two copies means two sets of module state"
        )


def test_the_import_that_broke_is_covered() -> None:
    """The concrete caller, not just the mechanism: baton-spec's generate.py."""
    from baton.integrations.mcp._compat import MCPServerClass

    from baton.integrations.official._compat import MCPServerClass as Renamed

    assert MCPServerClass is Renamed


def test_the_console_imports_it_from_the_adapter_path() -> None:
    """``derive_annotation_tool_name`` is live API at FOUR paths, and the
    callers that pin it are in another repo.

    ``baton-console``'s ``test_vendor_slug.py`` and ``test_onboarding_mcp.py``
    both do ``from baton.integrations.standalone.annotation import
    derive_annotation_tool_name``; the shim docstrings name the ``fastmcp``
    spelling of the same path.

    ⚠ Written because the body moved to ``_annotation_name`` during a cleanup
    and ruff's F401 autofix then deleted the re-export as "unused". Every test
    in this repo stayed green — the break was only reachable from the other
    repo. A re-export nothing here calls needs a test that calls it.
    """
    from baton.integrations.fastmcp.annotation import derive_annotation_tool_name as via_fastmcp
    from baton.integrations.mcp.annotation import derive_annotation_tool_name as via_mcp

    from baton.integrations.official.annotation import derive_annotation_tool_name as via_official
    from baton.integrations.standalone.annotation import (
        derive_annotation_tool_name as via_standalone,
    )

    assert via_standalone is via_fastmcp is via_official is via_mcp
    # Called, not merely imported: a re-export of the wrong object still imports.
    assert via_standalone("toybox") == "toybox_annotate"
