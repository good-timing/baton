"""``surface_snapshot.capabilities`` keeps the protocol's wire names on every mcp major.

mcp 2.x declares capability fields snake_case, with the camelCase wire name as
the ALIAS. A plain ``model_dump`` therefore emitted ``list_changed`` where the
protocol — and SPEC §11.4.2's own example — says ``listChanged``, and one server
hashed to two different surfaces depending on its mcp major.

Here rather than beside ``_surface.py``'s other callers because
``mcp-matrix`` runs only ``tests/integrations/official/``, so every mcp leg
executes it. On 1.x the field names are already camelCase and it passes either
way; the 2.x legs are the ones that discriminate.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from mcp.types import ServerCapabilities, ToolsCapability

from baton.integrations._surface import build_server_meta


def test_capabilities_are_dumped_under_their_wire_names() -> None:
    # A real library model: the alias behaviour lives in it, so a stand-in dict
    # would pass on every version and test nothing.
    capabilities = ServerCapabilities(tools=ToolsCapability(listChanged=False))
    server = SimpleNamespace(
        create_initialization_options=lambda: SimpleNamespace(
            server_name="surface-test",
            server_version="1.0.0",
            capabilities=capabilities,
            instructions=None,
        )
    )

    meta = build_server_meta(server)

    assert meta["capabilities"]["tools"] == {"listChanged": False}
    assert "list_changed" not in json.dumps(meta)
