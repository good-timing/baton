"""The public API (README, "What is public"). From 1.0 removing a name here is
a 2.0, and adding one is a promise."""

from __future__ import annotations

import importlib
import typing

import pytest

import baton
from baton.events import EventType


def _pascal(event_type: str) -> str:
    return "".join(word.title() for word in event_type.split("_"))


PUBLIC = {
    "baton": [
        "AsyncClient",
        "AsyncTrace",
        "Baton",
        "BatonHandle",
        "Client",
        "INTENT_INSTRUCTIONS",
        "INTENT_PARAMS",
        "IntentParam",
        "Organization",
        "Principal",
        "PrincipalResolutionContext",
        "ReportTool",
        "ResolvePrincipalHook",
        "ResultCaptureMode",
        "Trace",
        "VendorConfig",
        "__version__",
        "create_baton",
        "install_baton",
        "principal_from_oauth_email",
        "principal_from_oauth_sub",
        "report_tool",
    ],
    "baton.client": ["AsyncClient", "AsyncTrace", "Client", "Trace"],
    # Two classes per event type, so a new event type is required by existing.
    "baton.events": [
        "CaptureObserved",
        "Event",
        "EventType",
        "OrganizationWire",
        "PrincipalWire",
        *(
            _pascal(t) + suffix
            for t in typing.get_args(EventType)
            for suffix in ("Event", "Payload")
        ),
    ],
    "baton.identity": ["Organization", "Principal"],
    "baton.scrub": ["Scrubber", "identity_scrub"],
    "baton.sinks": ["FileSink", "HttpSink", "MultiSink", "Sink", "SinkClosedError", "StdoutSink"],
    **{
        adapter: [
            "BatonHandle",
            "PrincipalResolutionContext",
            "SessionResolutionContext",
            "VendorConfig",
            "install_baton",
        ]
        # The old paths baton.integrations.mcp and .fastmcp match these:
        # test_import_path_aliases.
        for adapter in ("baton.integrations.official", "baton.integrations.standalone")
    },
}


@pytest.mark.parametrize("module", sorted(PUBLIC))
def test_a_public_module_exports_exactly_its_names(module: str) -> None:
    mod = importlib.import_module(module)
    assert sorted(mod.__all__) == sorted(PUBLIC[module])
    for name in mod.__all__:
        assert hasattr(mod, name), f"{module}.{name}"


def test_the_old_name_of_the_hook_context_is_the_same_class() -> None:
    from baton import PrincipalResolutionContext
    from baton.integrations.official import SessionResolutionContext

    assert SessionResolutionContext is PrincipalResolutionContext


def test_the_hook_type_resolves_outside_the_sdk() -> None:
    def hook_taker(hook: baton.ResolvePrincipalHook) -> None: ...

    typing.get_type_hints(hook_taker)
