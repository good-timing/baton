"""The display name taken from the server object, on both adapters.

A DSN-only install named the vendor by the DSN's server segment, so a stranger
who wrapped ``MCPServer("toybox-pantry")`` shipped instructions reading
"wrapped in the srv-51885073 usage and friction SDK ... when a srv-51885073
tool call goes wrong". 0.8.3 already took the TOOL name from the server; 0.8.5
takes the display name from the same place, verbatim, behind the same guards.
``baton-ts`` implements the same rule.

The install tests assert on what an agent is SERVED (the rendered instructions
and the annotation tool's description), compared whole against the template:
a display name that resolves correctly and never reaches the text is the same
outcome for the vendor as one that resolves wrongly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.integrations._annotation_name import (
    annotation_tool_name_from_server,
    resolve_annotation_names,
)
from baton.integrations._config import VendorConfig
from baton.integrations._handle import BatonHandle
from baton.integrations._llm_text import (
    build_annotation_tool_description,
    build_server_instructions,
)
from baton.sinks import FileSink

WORKSPACE = "ten_655b084e118b43f88992ee6357fcc23c"
KEY = "baton_pk_" + "z" * 43
#: What the console mints, and what the stranger's instructions said.
SEGMENT = "srv-51885073"

#: 30 characters, the slug cap, so its tool name is the longest one derived
#: (39). In proactive mode the pair renders to exactly the 1500-char cap.
THIRTY = "Toybox Pantry Inventory Keeper"
THIRTY_TOOL = "toybox-pantry-inventory-keeper_annotate"
#: One character past it. The slug cap cuts the extra "s", so the tool name is
#: the same and only the display name grows.
ONE_PAST = THIRTY + "s"


@pytest.fixture
def dsn(httpserver: HTTPServer) -> str:
    """A DSN whose collector answers, so anything written while the surface is
    listed is accepted rather than retried with backoff."""
    httpserver.expect_request("/v0/events", method="POST").respond_with_response(
        Response(status=202)
    )
    origin = httpserver.url_for("").rstrip("/")
    scheme, _, authority = origin.partition("://")
    return f"{scheme}://{KEY}@{authority}/{WORKSPACE}/{SEGMENT}"


@dataclass
class _Served:
    """What an agent reads after install."""

    handle: BatonHandle
    instructions: str
    descriptions: dict[str, str]

    @property
    def annotate_description(self) -> str:
        return self.descriptions[self.handle.annotation_tool_name]


async def _official(
    name: str | None, config: VendorConfig | None = None, dsn: str | None = None
) -> _Served:
    from baton.integrations.official import install_baton
    from baton.integrations.official._compat import MCPServerClass

    mcp = MCPServerClass() if name is None else MCPServerClass(name)
    handle = install_baton(mcp, config, dsn=dsn)
    tools = await mcp.list_tools()
    return _Served(handle, mcp.instructions or "", {t.name: t.description or "" for t in tools})


async def _standalone(
    name: str | None, config: VendorConfig | None = None, dsn: str | None = None
) -> _Served:
    from fastmcp import FastMCP

    from baton.integrations.standalone import install_baton

    mcp = FastMCP() if name is None else FastMCP(name)
    handle = install_baton(mcp, config, dsn=dsn)
    # 2.x exposes ``get_tools()`` (a name->Tool dict), 3.x/4.x ``list_tools()``.
    list_tools = getattr(mcp, "list_tools", None)
    tools = (
        list(await list_tools())
        if list_tools is not None
        else list((await mcp.get_tools()).values())
    )
    return _Served(handle, mcp.instructions or "", {t.name: t.description or "" for t in tools})


Install = Callable[..., Awaitable[_Served]]

both_adapters = pytest.mark.parametrize(
    "install", [_official, _standalone], ids=["official", "standalone"]
)


@both_adapters
async def test_a_dsn_only_install_names_the_vendor_by_the_server(
    install: Install, dsn: str
) -> None:
    """The stranger's install exactly: a named server and a DSN, nothing else."""
    served = await install("toybox-pantry", dsn=dsn)
    try:
        assert served.handle.annotation_tool_name == "toybox-pantry_annotate"
        assert served.instructions == build_server_instructions(
            vendor_display_name="toybox-pantry",
            annotation_tool_name="toybox-pantry_annotate",
        )
        assert served.annotate_description == build_annotation_tool_description(
            vendor_display_name="toybox-pantry"
        )
        # The readable negative: the segment reaches nothing an agent reads.
        assert SEGMENT not in served.instructions
        assert SEGMENT not in served.annotate_description
    finally:
        await served.handle.aclose()


@both_adapters
@pytest.mark.parametrize("server_name", ["fastmcp", None], ids=["named-fastmcp", "unnamed"])
async def test_a_library_placeholder_keeps_the_dsn_segment_in_both_places(
    install: Install, dsn: str, server_name: str | None
) -> None:
    """``fastmcp`` is a library's name, not the vendor's, and so is whatever
    the library calls a server nobody named (``FastMCP-<4 hex>``, ``FastMCP``,
    ``mcp-server``). Neither name moves off what it was before 0.8.5."""
    served = await install(server_name, dsn=dsn)
    try:
        assert served.handle.annotation_tool_name == f"{SEGMENT}_annotate"
        assert served.instructions == build_server_instructions(
            vendor_display_name=SEGMENT, annotation_tool_name=f"{SEGMENT}_annotate"
        )
        assert served.annotate_description == build_annotation_tool_description(
            vendor_display_name=SEGMENT
        )
    finally:
        await served.handle.aclose()


@both_adapters
@pytest.mark.parametrize(
    ("overrides", "display", "tool"),
    [
        (
            {"vendor_display_name": "Toybox Pantry", "annotation_tool_name": "pantry_annotate"},
            "Toybox Pantry",
            "pantry_annotate",
        ),
        ({"vendor_display_name": "Toybox Pantry"}, "Toybox Pantry", "toybox-pantry_annotate"),
        ({"annotation_tool_name": "pantry_annotate"}, "toybox-pantry", "pantry_annotate"),
        # Explicit even though it equals the segment. The adapter reads "was it
        # given" before the DSN fills it in, because afterwards the two are the
        # same string and comparing against the segment would overrule this.
        ({"vendor_display_name": SEGMENT}, SEGMENT, "toybox-pantry_annotate"),
    ],
    ids=["both", "display-only", "tool-only", "display-equal-to-the-segment"],
)
async def test_an_explicit_name_wins(
    install: Install, dsn: str, overrides: dict[str, str], display: str, tool: str
) -> None:
    served = await install("toybox-pantry", VendorConfig(dsn=dsn, **overrides))
    try:
        assert served.handle.annotation_tool_name == tool
        assert served.instructions == build_server_instructions(
            vendor_display_name=display, annotation_tool_name=tool
        )
        assert served.annotate_description == build_annotation_tool_description(
            vendor_display_name=display
        )
    finally:
        await served.handle.aclose()


@both_adapters
async def test_without_a_dsn_nothing_changes(
    install: Install, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The display name stays the vendor's to give, and stays required: the
    server's name is only a default for what a DSN would otherwise supply."""
    monkeypatch.delenv("BATON_DSN", raising=False)
    served = await install(
        "toybox-pantry",
        VendorConfig(
            vendor_id="acme",
            vendor_display_name="Acme",
            sink=FileSink(str(tmp_path / "events.jsonl")),
        ),
    )
    try:
        assert served.instructions == build_server_instructions(
            vendor_display_name="Acme", annotation_tool_name="toybox-pantry_annotate"
        )
    finally:
        await served.handle.aclose()

    with pytest.raises(ValueError, match="vendor_display_name"):
        await install("toybox-pantry", VendorConfig(vendor_id="acme"))


@both_adapters
async def test_a_thirty_character_name_fits_in_the_tightest_mode(
    install: Install, dsn: str
) -> None:
    """Proactive mode interpolates the tool name 4x and the display name 5x,
    so it is where the two compete for the cap."""
    assert len(THIRTY) == 30
    served = await install(THIRTY, VendorConfig(dsn=dsn, proactive_mode="on"))
    try:
        assert served.handle.annotation_tool_name == THIRTY_TOOL
        assert served.instructions == build_server_instructions(
            vendor_display_name=THIRTY, annotation_tool_name=THIRTY_TOOL, proactive_mode="on"
        )
    finally:
        await served.handle.aclose()


@both_adapters
async def test_a_name_that_would_not_fit_falls_back_for_the_display_name(
    install: Install, dsn: str
) -> None:
    """One character past the case above, and the case that decides the order.

    The server's name fits beside ``srv-51885073_annotate`` and does not fit
    beside the readable tool name, so only one of the two can be readable. The
    tool name keeps it: it is the one 0.8.4 already gave this install, so
    nobody's tool renames on upgrade, and the display name falls back to the
    segment exactly as before.
    """
    # Preconditions, so a template change reads as this and not as a regression.
    with pytest.raises(ValueError):
        build_server_instructions(
            vendor_display_name=ONE_PAST, annotation_tool_name=THIRTY_TOOL, proactive_mode="on"
        )
    build_server_instructions(
        vendor_display_name=ONE_PAST,
        annotation_tool_name=f"{SEGMENT}_annotate",
        proactive_mode="on",
    )

    served = await install(ONE_PAST, VendorConfig(dsn=dsn, proactive_mode="on"))
    try:
        assert served.handle.annotation_tool_name == THIRTY_TOOL
        assert served.instructions == build_server_instructions(
            vendor_display_name=SEGMENT, annotation_tool_name=THIRTY_TOOL, proactive_mode="on"
        )
        assert served.annotate_description == build_annotation_tool_description(
            vendor_display_name=SEGMENT, proactive_mode="on"
        )
    finally:
        await served.handle.aclose()


# ---------------------------------------------------------------------------
# The resolver on its own, against stand-in server objects.


class _Server:
    """Stands in for a server object: the only thing read is ``.name``."""

    def __init__(self, name: object) -> None:
        self.name = name


class FastMCP(_Server):
    """Named to match the real class, because the placeholder rule compares
    the server's name against its own ``type(...).__name__``."""


class _Hostile:
    @property
    def name(self) -> str:
        raise RuntimeError("vendor property blew up")


def _resolved(**overrides: Any) -> VendorConfig:
    """A config shaped like ``resolve_config``'s output for a DSN-only install,
    minus the sink it would build: the segment in both identity slots."""
    return VendorConfig(
        vendor_id=SEGMENT,
        vendor_display_name=SEGMENT,
        dsn=f"https://{KEY}@h.example.com/{WORKSPACE}/{SEGMENT}",
        **overrides,
    )


def test_the_display_name_is_verbatim_and_the_tool_name_is_slugged() -> None:
    """The display name is the vendor's own string and reaches their users;
    the tool name has a pattern to satisfy. Neither is the other's copy."""
    names = resolve_annotation_names(
        FastMCP("Toybox Pantry (beta)"), _resolved(), display_name_given=False
    )
    assert names == ("Toybox Pantry (beta)", "toybox-pantry-beta_annotate")


def test_the_server_name_is_read_once() -> None:
    """Both names come from one read. ``.name`` may be a vendor property that
    does anything, including answer differently the second time."""
    reads: list[int] = []

    class Counting:
        @property
        def name(self) -> str:
            reads.append(1)
            return "toybox-pantry"

    names = resolve_annotation_names(Counting(), _resolved(), display_name_given=False)
    assert names == ("toybox-pantry", "toybox-pantry_annotate")
    assert len(reads) == 1


@pytest.mark.parametrize(
    "server",
    [
        object(),
        _Server(123),
        _Server(None),
        _Hostile(),
        FastMCP(""),
        FastMCP("   "),
        FastMCP("fastmcp"),
        FastMCP("FastMCP-1a2b"),
    ],
    ids=[
        "no-name",
        "not-a-string",
        "none",
        "throwing-read",
        "empty",
        "blank",
        "placeholder-literal",
        "placeholder-minted",
    ],
)
def test_an_unusable_name_keeps_the_segment_and_does_not_raise(server: object) -> None:
    names = resolve_annotation_names(server, _resolved(), display_name_given=False)
    assert names == (SEGMENT, f"{SEGMENT}_annotate")


@pytest.mark.parametrize("proactive_mode", ["off", "on"])
def test_no_server_name_turns_a_booting_install_into_a_raise_or_a_rename(
    proactive_mode: str,
) -> None:
    """Swept over every length from 1 to 120 in both modes, for two properties.

    The pair always renders, so a cosmetic default never stops a boot. And the
    tool name is the one 0.8.4 derived beside the DSN segment, so no install
    renames its tool on upgrade. The display name is the only thing that moves.
    """
    config = _resolved(proactive_mode=proactive_mode)
    moved = 0
    for n in range(1, 121):
        server = FastMCP("p" * n)
        names = resolve_annotation_names(server, config, display_name_given=False)
        build_server_instructions(
            vendor_display_name=names.vendor_display_name,
            annotation_tool_name=names.annotation_tool_name,
            proactive_mode=proactive_mode,
        )
        before = (
            annotation_tool_name_from_server(
                server, vendor_display_name=SEGMENT, proactive_mode=proactive_mode
            )
            or f"{SEGMENT}_annotate"
        )
        assert names.annotation_tool_name == before, n
        if names.vendor_display_name != SEGMENT:
            assert names.vendor_display_name == server.name
            moved += 1
    # Anchored: without it the sweep passes if the display name never moves.
    assert moved >= 30, f"the display name moved for only {moved} lengths"
