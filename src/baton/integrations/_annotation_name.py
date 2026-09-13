"""The annotation tool's name, resolved from the server object the SDK is handed.

``install_baton(mcp, ...)`` receives the vendor's server object and that object
knows its own name, so a readable tool name needs nothing from the vendor and
nothing from the wire. The default without it is ``{vendor_id}_annotate``, and
``vendor_id`` is an opaque ``srv-<8 hex>`` the console mints — which every
agent listing the vendor's tools reads as noise.

Design note: ``annotation_tool_name_from_the_server_object.md``.

**This composes a cosmetic LOCAL LABEL, never an id.** Identity stays opaque
and console-owned; nothing here reaches the wire.

⚠ **Nothing in this module may raise.** An annotation tool name is cosmetic and
a vendor's boot is not, so every path that cannot produce a name returns
``None`` and the caller falls back to ``{vendor_id}_annotate``. The one raise
that stays is the existing validator's, on a name the VENDOR passed explicitly
— that is their input and it should fail loud.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from baton.integrations._llm_text import build_server_instructions

if TYPE_CHECKING:
    from baton.integrations._config import VendorConfig

logger = logging.getLogger(__name__)

#: The strict cross-runtime client pattern. Owned here so both adapters'
#: ``derive_annotation_tool_name`` share one copy rather than three.
TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_SUFFIX = "_annotate"

#: Cap on the SLUG, and the binding constraint is NOT this pattern's 64.
#:
#: The tool name is interpolated into the server instructions 4x in proactive
#: mode, and ``build_server_instructions`` raises above a 1500-char cap — so
#: the tool name and ``vendor_display_name`` share one budget and a longer name
#: buys a shorter legal display name. At this cap a 39-char tool name leaves 30
#: characters of display name.
#:
#: ⚠ **The cap alone is NOT the safety property, and reasoning that it was is
#: how this shipped a boot failure in review.** "A display name that fits the
#: cap gets a tool name that fits too" is true and useless: nothing anywhere
#: enforces a 30-char ``vendor_display_name``. A vendor with a SHORT
#: ``vendor_id`` and a long display name — ``vendor_id="acme"`` gives a 13-char
#: ``acme_annotate`` — has budget today that a 39-char derived name spends,
#: so deriving could turn a working install into a ``ValueError`` at import.
#: ``_fits_the_instructions_budget`` below is the actual guard; the cap just
#: keeps the common case well clear of it.
SLUG_CAP = 30

#: What the libraries call a server nobody named, as fixed strings: the
#: official SDK's default is ``"FastMCP"`` on mcp 1.x and ``"mcp-server"`` on
#: 2.x. Placeholders, not names — slugging one produces a label that says
#: nothing about the vendor.
_PLACEHOLDER_LITERALS = frozenset({"fastmcp", "mcp-server"})

#: fastmcp's default is worse than generic. ``FastMCP.generate_name`` composes
#: ``f"{cls.__name__}-{secrets.token_hex(2)}"``, minted per CONSTRUCTION, so a
#: name derived from it would change on every restart — giving up the one
#: property (stability) that the opaque default actually has.
#:
#: Matched against the server's OWN class name rather than a literal
#: ``FastMCP`` prefix, because ``generate_name`` reads ``cls.__name__`` and a
#: vendor SUBCLASS therefore yields ``MyServer-1a2b``. And rather than a bare
#: trailing ``-[0-9a-f]{4}``, because hex spells words: ``toybox-cafe`` and
#: ``payments-face`` are real names that rule would throw away.
_RANDOM_SUFFIX = re.compile(r"-[0-9a-f]{4}\Z")
_COLLAPSE_HYPHENS = re.compile(r"-+")


def _is_library_placeholder(server_name: str, class_name: str) -> bool:
    """Is ``server_name`` a name the LIBRARY invented, rather than the vendor?"""
    stripped = server_name.strip()
    if stripped.lower() in _PLACEHOLDER_LITERALS:
        return True
    stem = _RANDOM_SUFFIX.sub("", stripped)
    return stem != stripped and stem == class_name


def slug_server_name(server_name: str) -> str | None:
    """Slug ``server_name`` for use as a tool-name stem, or ``None`` if unusable.

    Deliberately the same transform as the console's
    ``onboarding.vendor_slug.slug_base`` — lowercase, every character outside
    ``[a-z0-9-]`` to a hyphen, collapse, strip, cap. The cap differs (that one
    is set by ``vendor_id``'s 48-char limit, this one by the instructions
    budget above) and this one returns ``None`` where the console substitutes
    ``"server"``: a tool called ``server_annotate`` is no more readable than the
    opaque default and is likelier to collide with a real tool.
    """
    lowered = server_name.strip().lower()
    if not lowered:
        return None
    mapped = "".join(
        c if ("a" <= c <= "z" or "0" <= c <= "9" or c == "-") else "-" for c in lowered
    )
    collapsed = _COLLAPSE_HYPHENS.sub("-", mapped).strip("-")
    # Cut BEFORE the final strip: slicing can leave a trailing hyphen, which
    # reads as a truncation artefact.
    return collapsed[:SLUG_CAP].strip("-") or None


def _fits_the_instructions_budget(
    candidate: str, vendor_display_name: str, proactive_mode: str
) -> bool:
    """Would this name still let ``build_server_instructions`` render?

    Asked by RENDERING rather than by arithmetic, so the answer cannot drift
    from the template it is about: the interpolation count and the cap both
    live in ``_llm_text`` and both have moved before.

    ⚠ ``ValueError`` ONLY, which is the cap. A broader catch would read a
    template ``KeyError`` — our own bug — as "does not fit" and silently demote
    every vendor to the opaque name behind an ``info`` log. That failure must
    be loud, and it surfaces anyway at the real render a few lines later in
    ``install``.
    """
    try:
        build_server_instructions(
            vendor_display_name=vendor_display_name,
            annotation_tool_name=candidate,
            proactive_mode=proactive_mode,
        )
        return True
    except ValueError:
        return False


def annotation_tool_name_from_server(
    server: Any, *, vendor_display_name: str, proactive_mode: str
) -> str | None:
    """A readable ``{slug}_annotate`` from ``server.name``, or ``None``.

    Best-effort by contract. Returns only a name it has already run the
    validator over, so the caller can pass the result straight in as an
    override without that becoming a second raise site.

    ``.name`` is the PUBLIC attribute and is the only accessor that holds
    across the supported band — measured on all six E7 resolves, both adapters.
    The private ``_mcp_server`` that ``build_server_meta`` reaches through is
    renamed to ``_lowlevel_server`` on mcp 2.x, so this takes that helper's
    best-effort POSTURE without taking its accessor.

    ⚠ **The budget arguments are load-bearing, not decoration.** A derived name
    is longer than a short ``{vendor_id}_annotate``, and the server
    instructions have a hard cap that RAISES — so without checking, this
    cosmetic default could stop a server that booted before the upgrade. The
    rule is *never worse than the fallback*: a name that does not fit is
    discarded here, and the caller then composes ``{vendor_id}_annotate``
    exactly as it did before. If THAT does not fit either, the resulting raise
    is the pre-existing one, unchanged and with its own message.
    """
    try:
        raw = getattr(server, "name", None)
    except Exception:
        # Fail-open at a third-party boundary, and NARROW to the one operation
        # that warrants it: ``server`` is the vendor's object and ``.name`` may
        # be a property that does anything at all. Everything below is our own
        # string handling — if that raises, the bug is ours and should surface.
        logger.exception("baton: reading the server name failed; using the default")
        return None
    if not isinstance(raw, str):
        return None
    if _is_library_placeholder(raw, type(server).__name__):
        return None
    slug = slug_server_name(raw)
    if slug is None:
        return None
    candidate = f"{slug}{_SUFFIX}"
    # The slug's own alphabet is a subset of the pattern's and the cap is well
    # inside it, so this cannot currently fail. Checked anyway, because the
    # alternative to a check here is a raise at a vendor's import — and a
    # returned-but-unvalidated name is exactly what would get there.
    if not TOOL_NAME_PATTERN.match(candidate):
        return None
    if not _fits_the_instructions_budget(candidate, vendor_display_name, proactive_mode):
        logger.warning(
            "baton: the server-derived annotation tool name %r does not fit the "
            "server-instructions budget alongside vendor_display_name (%d chars); "
            "keeping %r. Shorten vendor_display_name to get the readable name.",
            candidate,
            len(vendor_display_name),
            "{vendor_id}_annotate",
        )
        return None
    return candidate


def derive_annotation_tool_name(vendor_id: str, override: str | None = None) -> str:
    """Compose and VALIDATE the final tool name. The last step of the ladder.

    Lived in both adapters' ``annotation.py`` until 2026-09-12, byte-identical
    in each. Re-exported from both of those paths, which are live API: the
    console's own tests import it as
    ``from baton.integrations.standalone.annotation import derive_annotation_tool_name``
    and the pre-rename shims cite that path in their docstrings.

    Raises ``ValueError`` if the resulting name violates the strict
    cross-runtime client pattern. This is the ONE raise site, and it is
    reached with a vendor's explicit name or with an already-validated derived
    one — never with a guess.
    """
    name = override or f"{vendor_id}{_SUFFIX}"
    if not TOOL_NAME_PATTERN.match(name):
        raise ValueError(
            f"Annotation tool name {name!r} violates the cross-runtime "
            f"pattern {TOOL_NAME_PATTERN.pattern!r} (Claude Desktop and others "
            f"reject names with dots or other separators)."
        )
    return name


def resolve_annotation_tool_name(server: Any, config: VendorConfig) -> str:
    """The whole ladder in one call: explicit → server-derived → ``vendor_id``.

    Both adapters call this once at install and thread the RESULT to the
    capture layer, the server instructions, the handle and the registration —
    so there is one resolution and no second site to drift from it. Sits with
    ``_resolve_tenant_id`` / ``resolve_sink`` in spirit: library-agnostic,
    reads only ``config`` and passes ``server`` through opaquely.
    """
    return derive_annotation_tool_name(
        config.vendor_id,
        config.annotation_tool_name
        or annotation_tool_name_from_server(
            server,
            vendor_display_name=config.vendor_display_name,
            proactive_mode=config.proactive_mode,
        ),
    )
