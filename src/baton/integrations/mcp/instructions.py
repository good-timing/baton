"""Server-instructions template for the official-mcp-SDK ``FastMCP``
``instructions`` field.

Thin re-export of the shared template — see ``baton.integrations._llm_text``
for the canonical copy.
"""

from __future__ import annotations

from baton.integrations._llm_text import build_server_instructions

__all__ = ["build_server_instructions"]
