"""Overlays in the settings document (SPEC §2.6): read, place, and change text."""

from __future__ import annotations

import functools
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from baton._white_space import WHITE_SPACE

logger = logging.getLogger("baton")

Place = Literal["audience", "server"]

INSTRUCTION_PIECES = ("instructions.subagent", "instructions.report_pointer")
_AUDIENCE_PIECES = ("goal_param.description", "report_tool.description", "report_tool.refusal")
_TEXT_CHANGES = ("append", "replace")
_FLAG_CHANGES = ("remove", "required")

# Overlay ids never change once saved, so one warning per action is enough.
_warned: set[tuple[str, int]] = set()
_unreadable: set[str] = set()


def warn_skipped(overlay_id: str, index: int, reason: str) -> None:
    if (overlay_id, index) in _warned:
        return
    _warned.add((overlay_id, index))
    logger.warning("baton: overlay %r action %d is skipped: %s", overlay_id, index, reason)


@dataclass(frozen=True)
class Action:
    index: int
    change: str
    text: str | None = None
    tool: str | None = None
    param: str | None = None
    piece: str | None = None
    instructions: bool = False


@dataclass(frozen=True)
class Overlay:
    id: str
    actions: tuple[Action, ...]

    @functools.cached_property
    def hidden_tools(self) -> frozenset[str]:
        return frozenset(
            a.tool for a in self.actions if a.tool and a.param is None and a.change == "remove"
        )

    def piece_actions(self, piece: str) -> tuple[Action, ...]:
        return tuple(a for a in self.actions if a.piece == piece)

    def instruction_appends(self) -> tuple[str, ...]:
        return tuple(a.text for a in self.actions if a.instructions and a.text is not None)


def changed_text(text: str | None, actions: tuple[Action, ...]) -> tuple[str | None, str]:
    """``text`` after ``actions``, and its ``agent_text`` source."""
    source = "default"
    for action in actions:
        if action.change == "replace":
            text, source = action.text, "replaced"
        elif action.change == "append" and action.text is not None:
            kept = (text or "").rstrip(WHITE_SPACE)
            text = f"{kept}\n\n{action.text}" if kept else action.text
            if source == "default":
                source = "appended"
    return text, source


def overlaid(text: str, overlay: Overlay | None, piece: str) -> str:
    """``text``, the piece ``piece`` of Baton's text, after ``overlay``."""
    if overlay is None:
        return text
    return changed_text(text, overlay.piece_actions(piece))[0] or text


def read_overlays(value: Any) -> Mapping[str, Any]:
    """The document's ``overlays``; one that is not an object holds none."""
    return value if isinstance(value, dict) else {}


def overlay_for(overlays: Mapping[str, Any], overlay_id: Any, place: Place) -> Overlay | None:
    """The overlay ``overlay_id`` names, holding the actions allowed in ``place``.
    ``None`` with one warning when it cannot be read; a bad action is skipped alone."""
    if overlay_id is None:
        return None
    if not isinstance(overlay_id, str):
        overlay_id = repr(overlay_id)
        body = None
    else:
        body = overlays.get(overlay_id)
    actions = body.get("actions") if isinstance(body, dict) else None
    if not isinstance(actions, list):
        if overlay_id not in _unreadable:
            _unreadable.add(overlay_id)
            logger.warning("baton: overlay %r cannot be read; none is applied there", overlay_id)
        return None
    kept = []
    for index, raw in enumerate(actions):
        action, reason = _action(index, raw, place)
        if action is None:
            warn_skipped(overlay_id, index, reason)
        else:
            kept.append(action)
    return Overlay(overlay_id, tuple(kept))


def _action(index: int, raw: Any, place: Place) -> tuple[Action | None, str]:
    if not isinstance(raw, dict) or not isinstance(target := raw.get("target"), dict):
        return None, "it has no target"
    changes = [c for c in (*_TEXT_CHANGES, *_FLAG_CHANGES) if c in raw]
    if len(changes) != 1 or set(raw) != {"target", changes[0]}:
        return None, "it needs exactly one change"
    change = changes[0]
    value = raw[change]
    if change in _TEXT_CHANGES and not isinstance(value, str):
        return None, f"{change!r} needs text"
    if change in _FLAG_CHANGES and value is not True:
        return None, f"{change!r} must be true"
    text = value if change in _TEXT_CHANGES else None
    keys = set(target)
    if keys == {"tool"} and isinstance(target["tool"], str):
        if change == "required":
            return None, "only a param can be required"
        return _placed(Action(index, change, text, tool=target["tool"]), place, "audience")
    if keys == {"tool", "param"} and all(isinstance(target[k], str) for k in keys):
        action = Action(index, change, text, tool=target["tool"], param=target["param"])
        return _placed(action, place, "audience")
    if keys == {"instructions"} and target["instructions"] is True:
        if change != "append":
            return None, "the instructions take only 'append'"
        return _placed(Action(index, change, text, instructions=True), place, "server")
    if keys == {"agent_text"} and isinstance(piece := target["agent_text"], str):
        if change not in _TEXT_CHANGES:
            return None, "agent_text takes only 'append' or 'replace'"
        if piece in INSTRUCTION_PIECES:
            return _placed(Action(index, change, text, piece=piece), place, "server")
        if piece in _AUDIENCE_PIECES:
            return _placed(Action(index, change, text, piece=piece), place, "audience")
        return None, f"unknown agent_text {piece!r}"
    return None, "unknown target"


def _placed(action: Action, place: Place, allowed: Place) -> tuple[Action | None, str]:
    if place != allowed:
        where = "server_overlay" if allowed == "server" else "an audience's overlay"
        return None, f"this target goes only in {where}"
    return action, ""
