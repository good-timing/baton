"""Compatibility alias — this adapter now lives at ``baton.integrations.official``.

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
than at a moment it chose. It is deleted once the console switches its import
lines — see the CHANGELOG entry for the rename.

The install extra is unchanged and still ``baton-sdk[mcp]``: extras name PyPI
distributions, not our folders, so the two deliberately differ now.
"""

from __future__ import annotations

from baton.integrations.official import (
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
