"""ASGI-scope test fixtures, shared so the wire form is described in ONE place.

Six copies of this scope literal had accumulated across the suite and they had
already drifted: some lowercased the header name before encoding it and some did
not, so a test could pass on one adapter purely because its fixture handed over a
spelling ASGI would never deliver. That is the same divergence register A8 is
about, re-created in the fixtures — which is why the lowercasing lives here and
takes no argument.

⚠ **Imports starlette, never fastmcp.** The ``mcp-matrix`` CI leg runs
``tests/integrations/official/`` with ``mcp`` installed and no ``fastmcp``, so a
shared helper that reached for fastmcp would break that leg. ``mcp`` hard-requires
starlette, so this import is safe there. ``set_http_request`` is fastmcp's and
stays at its call sites.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.requests import Request


def header_lines(headers: dict[str, str]) -> list[tuple[bytes, bytes]]:
    """Raw ASGI header lines, names lowercased the way the protocol delivers them.

    Takes a ``dict``, so it cannot express a repeated header; pass raw pairs
    directly where that matters (see ``fake_http_request``).
    """
    return [(name.lower().encode(), value.encode()) for name, value in headers.items()]


def fake_http_request(headers: dict[str, str] | list[tuple[bytes, bytes]]) -> Request:
    """A minimal ASGI-scope-backed Starlette ``Request`` carrying only headers —
    enough to drive fastmcp's ``get_http_headers()`` real contextvar path (via
    ``set_http_request``) without a real network socket.

    Accepts raw ``(name, value)`` pairs as well as a ``dict``, because a repeated
    header line is exactly what a proxy chain appends and a ``dict`` cannot hold
    two of them. Raw pairs are passed through verbatim — a caller reaching for
    that form is usually testing the wire, so it is not silently rewritten.
    """
    lines = header_lines(headers) if isinstance(headers, dict) else headers
    return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": lines})


def starlette_headers(headers: dict[str, str]) -> Headers:
    """Headers as the OFFICIAL adapter sees them, without building a request.

    ``Headers(raw=...)`` is the same object ``request.headers`` returns, so the
    surrounding ``Request`` is ceremony wherever only the headers are read.
    """
    return Headers(raw=header_lines(headers))
