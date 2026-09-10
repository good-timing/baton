"""The standalone ``fastmcp`` library's auth seam — one import, in one place.

Mirror of ``official/_auth.py``; see that module for why this is its own file.
fastmcp exposes its own dependency accessor, whose ``AccessToken`` carries the
same field set as the official SDK's from 2.14 up (verified 2.14.7 / 3.4.2 /
4.0.2). Returns ``None`` outside an authenticated HTTP request.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

get_access_token_or_none: Callable[[], Any] | None

try:
    from fastmcp.server.dependencies import get_access_token as get_access_token_or_none
except ImportError:  # pragma: no cover - defensive across the version band
    get_access_token_or_none = None
