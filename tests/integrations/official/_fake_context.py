"""Fake mcp ``Context`` objects, one per mcp MAJOR shape.

Shared because ``_extract_headers_from_context`` has two branches — mcp 2.0
exposes ``Context.headers``, mcp 1.x is reachable only through
``request_context.request.headers`` — and a second copy of that shape is a
second thing to edit when the access path moves again (``_mcp_server`` →
``_lowlevel_server`` already happened once). ``_FakeContextV1`` has no
``.headers`` attribute AT ALL, which is what mcp 1.x actually is; a stub that
sets it to ``None`` takes the same branch by luck rather than by fidelity.

Imports nothing, so it is safe in the ``mcp-matrix`` leg where ``fastmcp`` is
not installed.
"""

from __future__ import annotations

from typing import Any


class _FakeRequest:
    """Minimal stand-in for a transport request object — just headers."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _FakeRequestContext:
    def __init__(self, request: Any = None, meta: dict[str, Any] | None = None) -> None:
        self.request = request
        self.meta = meta


class _FakeContextV2:
    """Mimics mcp 2.0's ``Context``: a first-class ``.headers`` property.

    ⚠ **``.headers`` is NOT independent of ``request_context.request``, and this
    fake used to model it as if it were.** It set ``.headers`` while leaving
    ``request_context`` with no request at all, which is a state real mcp 2.x
    cannot reach: ``Context.headers`` there is literally
    ``getattr(self.request_context.request, "headers", None)`` (verified on mcp
    2.1.1). The split went unnoticed while every reader consulted ``.headers``
    first and stopped. ``observe_transport`` keys on the request object — as
    SPEC §11.4 requires, because headers are vacuously empty across the whole
    mcp 1.x band — so it read this fake as having no HTTP at all while the fake
    was meant to model an HTTP call. Attaching the request keeps the two in the
    relationship the library actually holds them in.
    """

    def __init__(
        self,
        headers: dict[str, str] | None,
        meta: dict[str, Any] | None = None,
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> None:
        self.headers = headers
        self.request_context = _FakeRequestContext(
            _FakeRequest(headers) if headers else None, meta=meta
        )
        # mcp>=2.0's MRTR properties — present directly on Context, not nested.
        # Both default None (a fresh, non-continuation call) so every existing
        # caller of this fake is unaffected.
        self.input_responses = input_responses
        self.request_state = request_state


class _FakeContextV1:
    """Mimics mcp 1.x's ``Context``: no ``.headers`` at all — only reachable
    through ``request_context.request.headers``."""

    def __init__(self, headers: dict[str, str] | None, meta: dict[str, Any] | None = None) -> None:
        self.request_context = _FakeRequestContext(
            _FakeRequest(headers) if headers else None, meta=meta
        )
