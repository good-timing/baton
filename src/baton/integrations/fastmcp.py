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

from baton.integrations.standalone import (
    BatonHandle,
    SessionResolutionContext,
    VendorConfig,
    install_baton,
)

__all__ = [
    "BatonHandle",
    "SessionResolutionContext",
    "VendorConfig",
    "install_baton",
]
