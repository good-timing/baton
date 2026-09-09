"""Unit tests for ``baton.integrations.runtime_adapter``.

The module had NO tests of its own before this file — not a gap in coverage
of an edge case, but zero. Its two functions were exercised only incidentally,
through one adapter's middleware suite, which is part of why the other adapter
never calling them went unnoticed through a rename and a release. The
end-to-end pair is ``tests/functional/test_agent_runtime_parity.py``; this
file pins the function's own contract, including the shapes an adapter can
hand it that are not dicts.
"""

from __future__ import annotations

from typing import Any

import pytest

from baton.integrations.runtime_adapter import (
    detect_agent_runtime,
    meta_to_dict,
)


class _FakeMetaModel:
    """Stands in for mcp's ``RequestParams.Meta``: a pydantic-ish object whose
    namespaced keys are ALIASES, so a dump without ``by_alias=True`` loses
    them. That is the whole reason ``meta_to_dict`` exists."""

    def __init__(self, by_alias_payload: dict[str, Any], plain_payload: dict[str, Any]) -> None:
        self._by_alias = by_alias_payload
        self._plain = plain_payload

    def model_dump(self, by_alias: bool = False) -> dict[str, Any]:
        return self._by_alias if by_alias else self._plain


class TestMetaToDict:
    def test_none_stays_none(self) -> None:
        assert meta_to_dict(None) is None

    def test_a_dict_passes_through_unchanged(self) -> None:
        meta = {"claudecode/toolUseId": "tu_1"}
        assert meta_to_dict(meta) is meta

    def test_a_model_is_dumped_BY_ALIAS(self) -> None:
        """The namespaced key only survives the alias dump.

        Asserted by giving the fake two different payloads rather than by
        trusting the call: a test that dumped either way would pass on a
        ``model_dump()`` with no ``by_alias``, and the namespaced keys every
        heuristic below keys on would be silently gone at runtime.
        """
        model = _FakeMetaModel(
            by_alias_payload={"claudecode/toolUseId": "tu_1"},
            plain_payload={"claudecode_toolUseId": "tu_1"},
        )
        assert meta_to_dict(model) == {"claudecode/toolUseId": "tu_1"}

    def test_an_object_that_is_neither_is_none(self) -> None:
        assert meta_to_dict(object()) is None


class TestDetectAgentRuntime:
    def test_no_meta_is_no_signal(self) -> None:
        assert detect_agent_runtime(None) is None

    def test_empty_meta_is_no_signal(self) -> None:
        assert detect_agent_runtime({}) is None

    def test_claudecode_prefix_is_the_heuristic(self) -> None:
        assert detect_agent_runtime({"claudecode/toolUseId": "tu_1"}) == "claude-code"

    def test_progress_token_alone_is_no_signal(self) -> None:
        """Cursor's shape per SPEC §5.2 — and Claude Code sends it too, so
        matching on it would attribute every Cursor call to Claude Code."""
        assert detect_agent_runtime({"progressToken": 7}) is None

    @pytest.mark.parametrize(
        "meta",
        [
            pytest.param({"io.baton/agent_runtime": "acme-plugin"}, id="reverse-dns-form"),
            pytest.param({"baton": {"agent_runtime": "acme-plugin"}}, id="pre-B5-nested-form"),
        ],
    )
    def test_no_client_override_is_honoured_in_any_form(self, meta: dict[str, Any]) -> None:
        """**The override was REMOVED 2026-09-09** — both spellings are inert.

        This is the point of the removal, so it is pinned rather than left to
        absence: a client asserting its own runtime is now ignored, and
        detection answers only from signals the SDK derives itself. The nested
        form died at B5 and the reverse-DNS form died here; a test that only
        covered one of them would let the other be quietly restored.
        """
        assert detect_agent_runtime(meta) is None

    def test_an_override_cannot_suppress_the_heuristic(self) -> None:
        """The removal must not have left a half-read key that can still lose
        us a detection — the heuristic answers regardless of what else is in
        ``_meta``."""
        meta = {"io.baton/agent_runtime": "acme-plugin", "claudecode/toolUseId": "tu_1"}
        assert detect_agent_runtime(meta) == "claude-code"

    def test_a_non_string_key_does_not_crash_the_prefix_scan(self) -> None:
        """``_meta`` arrives as JSON so keys are strings, but it is also handed
        to us as a plain dict by callers we do not control."""
        assert detect_agent_runtime({7: "x", "claudecode/toolUseId": "tu_1"}) == "claude-code"

    def test_it_accepts_a_model_not_only_a_dict(self) -> None:
        """The adapters pass the RAW ``_meta`` object straight in."""
        model = _FakeMetaModel(
            by_alias_payload={"claudecode/toolUseId": "tu_1"},
            plain_payload={},
        )
        assert detect_agent_runtime(model) == "claude-code"


class TestEverythingReturnedIsAValueWeControl:
    """No tier reads client text any more, so nothing here is scrubbed or
    capped. Pinned because that invariant is what licenses the absence of a
    cap — the next tier to read a client-supplied value (``clientInfo.name``)
    must bring its own."""

    def test_the_only_answer_is_the_sdks_own_constant(self) -> None:
        meta = {"claudecode/toolUseId": "tu_1", "io.baton/agent_runtime": "x" * 5000}
        assert detect_agent_runtime(meta) == "claude-code"

    def test_no_client_string_can_reach_the_return_value(self) -> None:
        """Any answer must be one of the SDK's own literals, whatever the
        client sent."""
        meta = {"claudecode/toolUseId": "tu_1", "attacker": "y" * 5000}
        assert detect_agent_runtime(meta) in {None, "claude-code"}
