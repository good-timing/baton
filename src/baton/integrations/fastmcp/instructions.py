"""Server-instructions template for the FastMCP ``instructions`` field.

Thin re-export of the shared template — see ``baton.integrations._llm_text``
for the canonical copy and the rationale for the instructions/description
split under Claude Code's truncation cap.

The MCP spec's ``InitializeResult`` carries a server-supplied ``instructions``
string that compliant clients SHOULD surface to the calling LLM. Empirically
load-bearing in Claude Code + Cursor; ignored by Claude Desktop and the
Claude.ai web MCP connector.
"""

from __future__ import annotations

from baton.integrations._llm_text import build_server_instructions

__all__ = ["build_server_instructions"]
