"""The library's ``list_tools``: the tool list a vendor serving MCP itself
hands its agents, and the ``surface_snapshot`` of the list it started from
(SPEC §5.6, §11.4.2)."""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from typing import Any, NamedTuple

from baton.events import SurfaceSnapshotPayload
from baton.integrations._llm_text import (
    USER_GOAL_PARAM_NAME,
    inject_goal_params,
    required_param_names,
    served_agent_text,
)
from baton.integrations._surface import assemble_surface, build_seam_augmentations, surface_hash

logger = logging.getLogger(__name__)

# The adapters' default. A library vendor has no config to change it.
INTENT_PARAM_MODE = "required"

Surface = tuple[dict[str, Any], str]

# A root that is not a plain object schema: a property added beside it may
# change what the schema accepts, so it gets no ``user_goal``.
_COMPOSITE_ROOT = ("$ref", "oneOf", "anyOf", "allOf")


class Served(NamedTuple):
    tools: list[Any]
    surface: Surface | None
    dispositions: dict[str, dict[str, str]]
    """Per tool name, as ``inject_goal_params`` returns them."""


def serve_tools(
    tools: list[Any],
    *,
    server_info: Mapping[str, Any] | None,
    capabilities: Mapping[str, Any] | None,
    instructions: str | None,
) -> Served:
    """``tools`` with ``user_goal`` added, each entry in the type it came in,
    and the surface of ``tools`` as passed with its hash. The surface shares
    the vendor's objects: copy it before it outlives this call. Never raises:
    a list it cannot read is served as it came, with no surface."""
    dispositions: dict[str, dict[str, str]] = {}
    try:
        vendor_true = [_as_json(tool) for tool in tools]
        served = [
            _served(tool, json, dispositions) for tool, json in zip(tools, vendor_true, strict=True)
        ]
    except Exception:
        logger.exception("baton: could not add user_goal to the tool list; serving it unchanged")
        return Served(tools, None, {})
    try:
        # Sorted so the same tools in another order hash the same, as in the adapters.
        ordered = sorted(
            vendor_true, key=lambda t: str(t.get("name")) if isinstance(t, Mapping) else ""
        )
        surface = assemble_surface(
            {
                "server_info": server_info,
                "capabilities": capabilities,
                "instructions": instructions,
            },
            ordered,
        )
        return Served(served, (surface, surface_hash(surface)), dispositions)
    except Exception:
        logger.exception("baton: surface snapshot capture failed")
        return Served(served, None, dispositions)


def snapshot_payload(
    surface: dict[str, Any], digest: str, seam: dict[str, Any] | None = None
) -> SurfaceSnapshotPayload:
    """The payload for ``surface``, deep-copied, since a queued event outlives
    the vendor's objects. ``seam`` defaults to ``list_tools()``'s, where the
    vendor serves Baton's text."""
    return SurfaceSnapshotPayload(
        surface_hash=digest,
        **copy.deepcopy(surface),
        seam_augmentations=seam
        if seam is not None
        else build_seam_augmentations(
            injected_tool_names=[],
            intent_param_names=[USER_GOAL_PARAM_NAME],
            intent_param_mode=INTENT_PARAM_MODE,
            required_names=list(required_param_names(intent_param_mode=INTENT_PARAM_MODE)),
            agent_text=served_agent_text(
                names=None,
                intent_param_mode=INTENT_PARAM_MODE,
                surface_tools=surface["tools"],
                vendor_serves_text=True,
            ),
        ),
    )


def _as_json(tool: Any) -> Any:
    """An MCP ``Tool`` model dumped by alias, so its schema keeps the wire name
    ``inputSchema``; anything else as it is."""
    if hasattr(tool, "model_dump") and not isinstance(tool, Mapping):
        return tool.model_dump(by_alias=True, mode="json", exclude_none=True)
    return tool


def _served(tool: Any, as_json: Any, dispositions: dict[str, dict[str, str]]) -> Any:
    if not isinstance(as_json, Mapping):
        return tool
    entry = dict(as_json)
    schema = entry.get("inputSchema")
    name = entry.get("name")
    if isinstance(schema, Mapping) and any(key in schema for key in _COMPOSITE_ROOT):
        if isinstance(name, str):
            dispositions[name] = {USER_GOAL_PARAM_NAME: "native"}
        return tool
    if isinstance(schema, Mapping):
        # Only these two levels change, so only they are copied.
        schema = dict(schema)
        if isinstance(schema.get("properties"), Mapping):
            schema["properties"] = dict(schema["properties"])
        if isinstance(schema.get("required"), list):
            schema["required"] = list(schema["required"])
        disposition = inject_goal_params(schema, intent_param_mode=INTENT_PARAM_MODE)
        if isinstance(name, str):
            dispositions[name] = disposition
        entry["inputSchema"] = schema
    if as_json is tool:
        return entry
    # A vendor's MCP library may read the entries it gets back as models.
    return type(tool).model_validate(entry)
