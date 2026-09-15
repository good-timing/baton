"""Two ``_meta`` dicts shaped exactly like ChatGPT's (``openai-mcp``), with every
private value replaced.

The keys, their order, the user agents, city / region / country / timezone and
``clientInfo`` are as ChatGPT sent them in prod on 2026-09-15. The coordinates
are the Ferry Building in San Francisco and the ids are placeholders: this repo
is public, so no prod coordinates and no prod ids go in a test, ever.

The two coordinate spellings are the two precisions seen on the wire: 5 decimals
from the Mac browser, 14 from the iPhone app. Both round to ``"37.8"`` /
``"-122.4"``.
"""

from __future__ import annotations

from typing import Any

#: ChatGPT in a Mac browser.
CHATGPT_MAC_META: dict[str, Any] = {
    "timezone": "America/Los_Angeles",
    "openai/locale": "en-US",
    "openai/session": "v1/placeholder-session",
    "openai/subject": "v1/placeholder-subject",
    "openai/userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "openai/organization": "v1/placeholder-org",
    "openai/userLocation": {
        "city": "San Carlos",
        "region": "California",
        "country": "US",
        "latitude": "37.79535",
        "timezone": "America/Los_Angeles",
        "longitude": "-122.39366",
    },
    "io.modelcontextprotocol/clientInfo": {"name": "openai-mcp", "version": "1.0.0"},
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {
        "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}},
        "experimental": {"openai/visibility": {"enabled": True}},
    },
}

#: The ChatGPT iPhone app.
CHATGPT_IPHONE_META: dict[str, Any] = {
    "timezone": "America/Los_Angeles",
    "openai/locale": "en-US",
    "openai/session": "v1/placeholder-session",
    "openai/subject": "v1/placeholder-subject",
    "openai/userAgent": "ChatGPT/1.2026.244 (iOS 26.6.2; iPhone18,2; build 33940143573)",
    "openai/organization": "v1/placeholder-org",
    "openai/userLocation": {
        "city": "Santa Clara",
        "region": "California",
        "country": "US",
        "latitude": "37.79535123456789",
        "timezone": "America/Los_Angeles",
        "longitude": "-122.39366123456789",
    },
    "io.modelcontextprotocol/clientInfo": {"name": "openai-mcp", "version": "1.0.0"},
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {
        "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}},
        "experimental": {"openai/visibility": {"enabled": True}},
    },
}
