"""Compatibility alias — this adapter now lives at ``baton.integrations.standalone``.

The package was renamed because its old name was a magnet for the wrong
choice: the OFFICIAL ``mcp`` SDK also names its server class ``FastMCP`` on
1.x, so a vendor on that library picked this folder on a class-name match and
got the adapter for a different project. Neither folder is named after a class
any more.

This module re-exports the package's public API so existing imports keep
working, and it does so SILENTLY: with no customers, the only reader of a
deprecation warning would be us, and the one consumer that matters
(``baton-console``) tracks ``baton-sdk`` by floor rather than by pin, so this
alias is what keeps a release from breaking it at the next resolve rather
than at a moment it chose. It is deleted once the console switches its import
lines — see the CHANGELOG entry for the rename.

The install extra is unchanged and still ``baton-sdk[fastmcp]``: extras name
PyPI distributions, not our folders, so the two deliberately differ now.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys

from baton.integrations import standalone as _target
from baton.integrations.standalone import (
    BatonHandle,
    SessionResolutionContext,
    VendorConfig,
    install_baton,
)

# Submodule imports at the old path — ``from baton.integrations.fastmcp.middleware
# import ...`` — need more than a re-export: a plain module is not a package, so
# the import machinery refuses to look inside it. Registering the RENAMED
# package's already-imported submodules under the old dotted names makes those
# imports resolve, and resolve to the same module objects rather than to second
# copies loaded from the same files. Discovered rather than listed, so a
# submodule added before the aliases are deleted is covered without anyone
# remembering to come back here.
#
# The sibling shim has a known external caller of this shape
# (``baton-spec/scripts/generate.py`` imports ``baton.integrations.mcp._compat``,
# vendored into baton, baton-proxy and baton-extmcp). None is known for this
# side, but ``middleware.BatonMiddleware`` is the obvious candidate — it is how
# this repo's own tests reach it — so both shims behave the same rather than
# waiting to find out.
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
