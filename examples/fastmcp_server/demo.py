"""Drive the wrapped bookmarks server in-process and show what Baton captured.

No network, no Console, no config — the server uses a StdoutSink, so every
captured event prints as JSONL to stderr.

    python examples/fastmcp_server/demo.py

Watch `tool_call_start`: it carries the *why* (and *what success looks like*)
as `call_intent` (intent_source=injected_param), captured from the
`user_goal` param even though this client never read server instructions.
That's the Claude Desktop case: instructions are ignored, intent still lands.
`params` holds exactly the vendor-visible arguments: the injected param was
stripped before the tool ran.
"""

from __future__ import annotations

import asyncio

from fastmcp import Client

# Importing the server module applies install_baton() as a side effect.
from server import mcp  # type: ignore[import-not-found]


async def main() -> None:
    async with Client(mcp) as client:
        # `user_goal` is the param Baton injected into every tool's schema. A real
        # agent fills it; here we pass it explicitly to simulate that.
        await client.call_tool(
            "save_bookmark",
            {
                "name": "onboarding",
                "url": "https://example.com/onboarding",
                "user_goal": (
                    "1: save the onboarding guide as a bookmark; "
                    "success is the bookmark saved and retrievable by name"
                ),
            },
        )
        await client.call_tool(
            "get_bookmark",
            {
                "name": "onboarding",
                "user_goal": "1: open the saved onboarding guide; success is its URL",
            },
        )

    print("\n^ Those JSON lines on stderr are the signal Baton captured.")


if __name__ == "__main__":
    asyncio.run(main())
