"""The official SDK's auth seam — one import, in one place.

Separate from ``_tool_wrap.py`` because BOTH emit paths need it (the tool wrap
and the annotation tool), and a private name re-exported through one of them is
the kind of accidental coupling that made ``runtime_adapter`` live under the
wrong package for two releases.

``get_access_token()`` reads a contextvar that MCP's bearer-auth ASGI
middleware sets, so it returns ``None`` outside an authenticated HTTP request —
including on every stdio call, where no auth exists at all. That is the normal
case, not an error.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: ``None`` when the auth module cannot be imported. Present across the whole
#: supported band (verified mcp 1.20 → 2.0); the guard covers a future move,
#: and it degrades to "identity is never resolved" rather than a broken import.
get_access_token_or_none: Callable[[], Any] | None

try:
    from mcp.server.auth.middleware.auth_context import (
        get_access_token as get_access_token_or_none,
    )
except ImportError:  # pragma: no cover - defensive across the version band
    get_access_token_or_none = None


def current_access_token() -> Any | None:
    """The verified token for the request in flight, or ``None``.

    Never raises. The accessor itself can: fastmcp's ``get_access_token()``
    ends in an explicit ``raise TypeError`` on its conversion path, reachable
    when a vendor's ``TokenVerifier`` returns a non-fastmcp ``AccessToken``
    whose ``model_dump()`` is missing a key it wants. Calling it in an argument
    expression put that raise OUTSIDE ``resolve_user_id``'s never-raise
    boundary, where it would reach the vendor's tool call — the same fail-open
    hole the runtime detector had one layer over.
    """
    if get_access_token_or_none is None:
        return None
    try:
        return get_access_token_or_none()
    except Exception:
        return None
