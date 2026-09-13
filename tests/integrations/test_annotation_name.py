"""The annotation tool name taken from the server object.

``vendor_id`` is an opaque ``srv-<8 hex>`` the console mints, so the old
default composed ``srv-…_annotate`` — what every agent listing the vendor's
tools reads. Design note:
``baton-internal/docs/design-notes/annotation_tool_name_from_the_server_object.md``.

Two properties carry the whole feature and each has a negative control here:

- a LIBRARY placeholder is not a name (fastmcp's is random per construction,
  so deriving from it would rename the tool on every restart), and
- nothing in this path may raise, because a cosmetic label must never stop a
  vendor's server from starting.
"""

from __future__ import annotations

import pytest

from baton.integrations._annotation_name import (
    SLUG_CAP,
    TOOL_NAME_PATTERN,
    annotation_tool_name_from_server,
    slug_server_name,
)
from baton.integrations._llm_text import build_server_instructions

#: Budget arguments for the tests that are not about the budget. ``"off"`` is
#: the default mode and a short display name leaves ~84 chars of tool name, so
#: nothing here is silently refused for a reason the test does not name.
_ROOMY = {"vendor_display_name": "V", "proactive_mode": "off"}


class _Server:
    """Stands in for a server object: the only thing read is ``.name``."""

    def __init__(self, name: object) -> None:
        self.name = name


class FastMCP(_Server):
    """Named to match the real class, because the placeholder rule compares
    the server's name against its own ``type(...).__name__``."""


class MCPServer(_Server):
    pass


class MyServer(FastMCP):
    """A vendor subclass — ``generate_name`` composes from ``cls.__name__``,
    so its placeholder is ``MyServer-<4 hex>`` and not ``FastMCP-<4 hex>``."""


@pytest.mark.parametrize(
    ("server_name", "expected"),
    [
        ("Echo Server", "echo-server_annotate"),
        ("toybox-pantry", "toybox-pantry_annotate"),
        ("Café / Naïve  Server!!", "caf-na-ve-server_annotate"),
        ("  Padded Name  ", "padded-name_annotate"),
        ("ACME Corp", "acme-corp_annotate"),
    ],
)
def test_a_real_name_becomes_a_readable_tool_name(server_name: str, expected: str) -> None:
    assert annotation_tool_name_from_server(FastMCP(server_name), **_ROOMY) == expected


@pytest.mark.parametrize(
    ("cls", "server_name"),
    [
        (FastMCP, "FastMCP-16e8"),
        (FastMCP, "FastMCP-1f78"),
        (FastMCP, "FastMCP"),
        (MCPServer, "mcp-server"),
        (MyServer, "MyServer-1a2b"),
    ],
)
def test_a_library_placeholder_is_refused(cls: type, server_name: str) -> None:
    """``None`` here is what makes the caller fall back to ``{vendor_id}_annotate``.

    fastmcp's default is ``f"{cls.__name__}-{secrets.token_hex(2)}"``, minted
    per CONSTRUCTION — deriving from it would give the tool a new name on every
    restart, giving up the one property the opaque default actually has.
    """
    assert annotation_tool_name_from_server(cls(server_name), **_ROOMY) is None


def test_the_placeholder_rule_does_not_eat_a_real_name() -> None:
    """The negative control for the rule above, and the reason it matches the
    server's own class name rather than a bare trailing ``-[0-9a-f]{4}``.

    Hex spells words. ``toybox-cafe`` and ``payments-face`` both end in four
    hex characters and are perfectly good names; the SAME string is a
    placeholder only when it is the class's own name.
    """
    assert (
        annotation_tool_name_from_server(FastMCP("toybox-cafe"), **_ROOMY) == "toybox-cafe_annotate"
    )
    assert (
        annotation_tool_name_from_server(FastMCP("payments-face"), **_ROOMY)
        == "payments-face_annotate"
    )
    # ``MyServer-1a2b`` on a FastMCP is a vendor who named their server that.
    assert (
        annotation_tool_name_from_server(FastMCP("MyServer-1a2b"), **_ROOMY)
        == "myserver-1a2b_annotate"
    )
    # ...and on a MyServer it is the library's placeholder.
    assert annotation_tool_name_from_server(MyServer("MyServer-1a2b"), **_ROOMY) is None


@pytest.mark.parametrize("server_name", ["!!!", "", "   ", "///", "---"])
def test_a_name_with_nothing_sluggable_yields_none(server_name: str) -> None:
    """Rather than the console's ``"server"`` fallback: ``server_annotate`` is
    no more readable than the opaque default and is likelier to collide."""
    assert annotation_tool_name_from_server(FastMCP(server_name), **_ROOMY) is None


def test_the_cap_strips_a_hyphen_the_cut_lands_on() -> None:
    """Capping AND the artefact strip, in the one input that separates them.

    ⚠ This replaces two tests that asserted the same half. Verified by
    mutation: deleting the post-cut ``.strip("-")`` left BOTH of them green,
    because neither input put a hyphen at the cut. ``"a" * 29 + " tail"``
    collapses to ``"a" * 29 + "-tail"``, so the 30-char cut lands exactly on
    the hyphen — which is the case the strip exists for
    → [[feedback_a_mutation_that_reds_nothing_may_mean_a_missing_test]].
    """
    assert slug_server_name("a" * (SLUG_CAP - 1) + " tail") == "a" * (SLUG_CAP - 1)

    # ...and the cap itself, on a name far past it.
    name = annotation_tool_name_from_server(FastMCP("A Very Long Server Name " * 10), **_ROOMY)
    assert name is not None
    assert len(name) == SLUG_CAP + len("_annotate")


@pytest.mark.parametrize(
    "server",
    [
        object(),  # no ``.name`` at all
        _Server(123),  # ``.name`` is not a string
        _Server(None),
    ],
)
def test_an_unusable_server_object_yields_none_rather_than_raising(server: object) -> None:
    assert annotation_tool_name_from_server(server, **_ROOMY) is None


def test_an_exploding_name_property_is_caught() -> None:
    """``server`` is the VENDOR's object and ``.name`` may be a property that
    does anything. A cosmetic label must not stop their server booting."""

    class Hostile:
        @property
        def name(self) -> str:
            raise RuntimeError("vendor property blew up")

    assert annotation_tool_name_from_server(Hostile(), **_ROOMY) is None


def test_every_derived_name_is_already_valid() -> None:
    """The caller passes this straight in as the override, so a name that
    failed the pattern would become a raise at the vendor's import.

    Swept rather than sampled: the transform's own alphabet is a subset of the
    pattern's, and this is the check that would notice if either moved.
    """
    awkward = [
        "Echo Server",
        "Café / Naïve  Server!!",
        "A" * 200,
        "…‽ Ünïcödé ‽…",
        "tabs\tand\nnewlines",
        "UPPER_snake-Mixed.dots",
        "9 leading digits",
        "-leading-and-trailing-",
        "emoji 🎺 server",
    ]
    produced = 0
    for raw in awkward:
        derived = annotation_tool_name_from_server(FastMCP(raw), **_ROOMY)
        if derived is not None:
            produced += 1
            assert TOOL_NAME_PATTERN.match(derived), (raw, derived)
    # Anchored: without this the whole sweep passes if the resolver starts
    # returning ``None`` for everything, which is the one way it could go
    # wrong and still look green.
    assert produced >= 4, f"only {produced} of {len(awkward)} names produced anything"


def test_deriving_never_turns_a_working_install_into_a_raise() -> None:
    """The property the SLUG_CAP comment used to claim, and did not have.

    The tool name enters the server instructions 4x under
    ``proactive_mode="on"`` against a hard cap that RAISES, so it shares a
    budget with ``vendor_display_name``. A derived name is LONGER than a short
    ``{vendor_id}_annotate`` — so without a guard this cosmetic default can
    stop a server that booted before the upgrade.

    The earlier version of this test asserted that a 30-char display name fits
    and a 31-char one does not. Both are true and neither protects anything:
    NOTHING enforces a 30-char ``vendor_display_name``
    → [[feedback_a_justification_must_name_the_case_it_covers]].
    """
    long_display = "Acme Corporation Knowledge Platform"  # 35 chars, no rule against it
    # The pre-change name for this vendor, which renders today.
    build_server_instructions(
        vendor_display_name=long_display,
        annotation_tool_name="acme_annotate",
        proactive_mode="on",
    )
    # The name the server WOULD supply is legal and readable...
    assert slug_server_name("Acme Corporation Knowledge Platform") is not None
    # ...and is refused anyway, because taking it would make the boot fail.
    assert (
        annotation_tool_name_from_server(
            FastMCP("Acme Corporation Knowledge Platform"),
            vendor_display_name=long_display,
            proactive_mode="on",
        )
        is None
    )


def test_a_name_that_fits_is_still_taken() -> None:
    """The negative control for the guard above — it must refuse names that
    would break the render, not every name."""
    assert (
        annotation_tool_name_from_server(
            FastMCP("Acme Corporation Knowledge Platform"),
            vendor_display_name="Acme",
            proactive_mode="on",
        )
        == "acme-corporation-knowledge-pla_annotate"
    )
    # And the SAME long display name keeps it in the default reactive-only
    # mode, where the name is interpolated 3x instead of 4x and there is room.
    assert (
        annotation_tool_name_from_server(
            FastMCP("Acme Corporation Knowledge Platform"),
            vendor_display_name="Acme Corporation Knowledge Platform",
            proactive_mode="off",
        )
        == "acme-corporation-knowledge-pla_annotate"
    )


def test_the_guard_does_not_rescue_a_fallback_that_never_fitted() -> None:
    """When ``{vendor_id}_annotate`` itself blows the budget, that raise is
    pre-existing and stays — the guard makes this change harmless, it does not
    take on fixing a config that was already broken."""
    huge = "A" * 120
    with pytest.raises(ValueError):
        build_server_instructions(
            vendor_display_name=huge,
            annotation_tool_name="acme_annotate",
            proactive_mode="on",
        )
    assert (
        annotation_tool_name_from_server(
            FastMCP("Acme Corporation Knowledge Platform"),
            vendor_display_name=huge,
            proactive_mode="on",
        )
        is None
    )
