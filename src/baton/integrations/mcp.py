r"""Compatibility alias — this adapter now lives at ``baton.integrations.official``.

The package was renamed because its old name was a magnet for the wrong
choice: the official ``mcp`` SDK's server class is called ``FastMCP`` on 1.x,
so a vendor scanning the folder list picked ``baton.integrations.fastmcp`` —
the STANDALONE library's adapter — on a class-name match. Neither folder is
named after a class any more.

This module re-exports the package's public API so existing imports keep
working, and it does so SILENTLY: with no customers, the only reader of a
deprecation warning would be us, and the one consumer that matters
(``baton-console``) tracks ``baton-sdk`` by floor rather than by pin, so this
alias is what keeps a release from breaking it at the next resolve rather
than at a moment it chose.

DO NOT delete these yet, and do NOT delete them on any single event. The
original plan said "delete once baton-console switches its imports"; the console
switched, and a sweep then showed that trigger was wrong — it named the one
consumer we happened to know about. A first attempt at replacing it with a
hand-written list of the others was ALSO wrong: it missed four old-path callers,
omitted baton-ts, and claimed this repo was clear when its own vendored
``baton-spec`` is not.

So the precondition is a CHECK, not a list, because a list drifts and this one
already did. From each repo root::

    grep -rn "baton\.integrations\.\(mcp\|fastmcp\)" . \
      --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules \
      --exclude=CHANGELOG.md

In THIS repo four hits are expected and are themselves part of the deletion
commit: these two shims, the sentence in ``integrations/__init__.py`` announcing
them, and ``tests/test_import_path_aliases.py``. Anything else is a blocker.
Delete the shims when the check is clean by that rule in:
``baton`` (this repo — INCLUDING the vendored ``baton-spec/`` submodule, which
is on the old paths at the pinned pointer), ``baton-spec``, ``baton-internal``
(the toybox fixture and the identity_probe spike are live; several spikes and
two READMEs that tell a reader to paste the old path are also hits), and
``baton-proxy`` / ``baton-extmcp`` / ``baton-ts``, each of which vendors
``baton-spec`` at a pointer whose ``scripts/generate.py`` still uses the old
paths, and ``baton-console``.

``baton-console`` was written here as "already clear", and that was the third
scope claim in this rename asserted instead of checked. Running the grep above
from ITS repo root returns three hits: ``backend/tests/test_vendor_slug.py`` and
``backend/tests/test_onboarding_mcp.py`` both do
``from baton.integrations.fastmcp.annotation import derive_annotation_tool_name``
— live imports that resolve only because these shims register submodules — and
``docs/DEMO_SINGLESTORE.md`` hands a reader ``from baton.integrations.mcp import
install_baton, VendorConfig`` to paste. Its switch commit (``82e4907``) moved
``src/`` and the emitted recipe, which is what "the console switched" meant; the
tests and that doc were never in it. So no repo is exempt from the check, and
this list names WHERE to run it, not what it will find.

Note the break does NOT wait for a release. ``baton-spec/scripts/generate.py``
is documented to run against THIS repo's editable ``.venv``, so deleting on
``main`` breaks that script the same day.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

from baton.integrations import official as _target
from baton.integrations.official import (
    BatonHandle,
    SessionResolutionContext,
    VendorConfig,
    install_baton,
)

# Submodule imports at the old path — ``from baton.integrations.mcp._compat
# import ...`` — need more than a re-export: a plain module is not a package, so
# the import machinery refuses to look inside it. Registering the RENAMED
# package's already-imported submodules under the old dotted names makes those
# imports resolve, and resolve to the same module objects rather than to second
# copies loaded from the same files. Discovered rather than listed, so a
# submodule added before the aliases are deleted is covered without anyone
# remembering to come back here.
#
# This is not hypothetical: ``baton-spec/scripts/generate.py`` imports
# ``baton.integrations.mcp._compat``, and that script is vendored into baton,
# baton-proxy and baton-extmcp.
for _sub in pkgutil.iter_modules(_target.__path__):
    sys.modules[f"{__name__}.{_sub.name}"] = importlib.import_module(
        f"{_target.__name__}.{_sub.name}"
    )

__all__ = [
    "BatonHandle",
    "SessionResolutionContext",
    "VendorConfig",
    "install_baton",
]
