"""Drive the wrapped bookmarks server in-process and show what Baton captured.

No network, no Console, no config — the server uses a StdoutSink, so every
captured event prints as JSONL to stderr.

    python examples/fastmcp_server/demo.py

Watch for two things in the output:
  1. `annotation` (intent_source=injected_param) — the *why* (and *what success
     looks like*), captured from the `user_goal`/`expected_result` params even
     though this client never read server instructions. That's the Claude
     Desktop case: instructions are ignored, intent still lands.
  2. `tool_call_start` / `tool_call_end` — the *what*, with `call_intent` riding
     the start event and `params` holding exactly the vendor-visible arguments
     (no `user_goal`/`expected_result` — both were stripped before the tool ran).
"""

from __future__ import annotations

import asyncio

from fastmcp import Client

# Importing the server module applies install_baton() as a side effect.
from server import mcp  # type: ignore[import-not-found]


async def main() -> None:
    async with Client(mcp) as client:
        # `user_goal`/`expected_result` are the params Baton injected into every
        # tool's schema. A real agent fills them; here we pass them explicitly
        # to simulate that.
        await client.call_tool(
            "save_bookmark",
            {
                "name": "onboarding",
                "url": "https://example.com/onboarding",
                "user_goal": "user is bookmarking the onboarding guide to find it later",
                "expected_result": "the bookmark is saved and retrievable by name",
            },
        )
        await client.call_tool(
            "get_bookmark",
            {
                "name": "onboarding",
                "user_goal": "user is retrieving the onboarding guide they saved",
            },
        )

    print("\n^ Those JSON lines on stderr are the signal Baton captured.")


if __name__ == "__main__":
    asyncio.run(main())
