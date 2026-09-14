"""The annotation tool's name, resolved from the server object the SDK is handed.

``install_baton(mcp, ...)`` receives the vendor's server object and that object
knows its own name, so a readable tool name needs nothing from the vendor and
nothing from the wire. The default without it is ``{vendor_id}_annotate``, and
``vendor_id`` is an opaque ``srv-<8 hex>`` the console mints — which every
agent listing the vendor's tools reads as noise.

Design note: ``annotation_tool_name_from_the_server_object.md``.

As of 0.8.5 the display name comes from the same place. A DSN-only install
used to name the vendor by the DSN's server segment, so the instructions an
agent reads said "wrapped in the srv-51885073 usage and friction SDK". Now it
is ``server.name`` VERBATIM (the vendor's own string, which reaches their
users, so it is not slugged), behind the same guards the tool name uses.
``resolve_annotation_names`` settles both in one call, and ``baton-ts``
implements the same rule.

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
from typing import TYPE_CHECKING, Any, NamedTuple

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
    server_name = _name_the_vendor_chose(server)
    if server_name is None:
        return None
    return _tool_name_within_budget(
        server_name, vendor_display_name=vendor_display_name, proactive_mode=proactive_mode
    )


def _name_the_vendor_chose(server: Any) -> str | None:
    """``server.name`` when the VENDOR chose it, else ``None``.

    The guards the tool name and the display name share, in one place so the
    two cannot disagree about what counts as a name: a read that throws, a
    value that is not a string, and a name the library invented.
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
    return raw


def _tool_name_candidate(raw: str) -> str | None:
    """``{slug}_annotate`` from a vendor-chosen name, validated, or ``None``.

    Not yet checked against the instructions budget: the display name is
    rendered beside this candidate before the tool name's own check runs.
    """
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
    return candidate


def _tool_name_within_budget(
    raw: str, *, vendor_display_name: str, proactive_mode: str
) -> str | None:
    """``_tool_name_candidate``, or ``None`` if it would not render beside
    ``vendor_display_name``."""
    candidate = _tool_name_candidate(raw)
    if candidate is None:
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


class AnnotationNames(NamedTuple):
    """The two LLM-facing names ``install_baton`` threads, in the order they settle."""

    vendor_display_name: str
    annotation_tool_name: str


def resolve_annotation_names(
    server: Any, config: VendorConfig, *, display_name_given: bool
) -> AnnotationNames:
    """Both names in one call: the display name FIRST, then the tool name.

    Both adapters call this once at install and thread the RESULT: the tool
    name to the capture layer, the server instructions, the handle and the
    registration; the display name to the server instructions and the
    annotation tool's description. One resolution, no second site to drift
    from it. Library-agnostic: reads only ``config``, passes ``server`` through
    opaquely, and reads ``server.name`` exactly once.

    **The display name** is ``config.vendor_display_name`` unless the vendor
    left it unset and a DSN filled it in. ``display_name_given`` says which,
    and the adapter reads it BEFORE ``build_config``: after it, a defaulted
    value and one the vendor typed are the same string, and an explicit name
    that happens to equal the DSN segment must still win. When the DSN filled
    it in, the display name is the server's name VERBATIM (the vendor's own
    string, which reaches their users, so it is not slugged). It falls back to
    the DSN segment on the tool name's guards (a library placeholder, a
    non-string, a read that throws), on a blank name, and when the server
    instructions would not render within the cap.

    **Why the display name goes first.** Each name's budget check needs the
    other name, so one of the two is checked against a name that is not final
    yet. The tool name's check has to see the REAL display text: checked
    against the 12-character DSN segment it could approve a 39-character tool
    name, the display name could then become a longer server name, and the
    render would raise at the vendor's import. So the display name settles
    first, and the tool-name ladder below runs unchanged against it.

    The display name is rendered beside the tool name that ladder is about to
    PROPOSE: the explicit override, else the server-derived name before its
    budget check, else ``{vendor_id}_annotate``. That decides who keeps the
    readable name when the budget has room for only one, and it is the tool
    name. So every install that booted on 0.8.4 keeps the tool name it had
    there, and the only thing this changes is the display name, wherever it
    fits beside that tool name.

    Every pair that uses the server's name has been rendered once already. Any
    other pair is one 0.8.4 produced too, and if it does not fit, the raise is
    the pre-existing one, unchanged.
    """
    server_name = _name_the_vendor_chose(server)

    vendor_display_name = config.vendor_display_name
    if not display_name_given and config.dsn and server_name is not None and server_name.strip():
        proposed = (
            config.annotation_tool_name
            or _tool_name_candidate(server_name)
            or f"{config.vendor_id}{_SUFFIX}"
        )
        if _fits_the_instructions_budget(proposed, server_name, config.proactive_mode):
            vendor_display_name = server_name
        else:
            logger.warning(
                "baton: the server name %r does not fit the server-instructions "
                "budget as vendor_display_name beside the annotation tool %r; "
                "keeping the DSN's server segment %r. Pass vendor_display_name= "
                "to name the server in fewer characters.",
                server_name,
                proposed,
                vendor_display_name,
            )

    annotation_tool_name = derive_annotation_tool_name(
        config.vendor_id,
        config.annotation_tool_name
        or (
            _tool_name_within_budget(
                server_name,
                vendor_display_name=vendor_display_name,
                proactive_mode=config.proactive_mode,
            )
            if server_name is not None
            else None
        ),
    )
    return AnnotationNames(vendor_display_name, annotation_tool_name)


def resolve_annotation_tool_name(server: Any, config: VendorConfig) -> str:
    """The tool name alone, budgeted against ``config.vendor_display_name`` as given.

    Install does not call this; it calls ``resolve_annotation_names``, which
    settles the display name first. Kept with its 0.8.4 behaviour because
    ``baton-console``'s tests import it by this path
    (``test_onboarding_recipes_execute.py``) and the console depends on
    ``baton-sdk`` by floor, so removing it would break them on upgrade.
    """
    return resolve_annotation_names(server, config, display_name_given=True).annotation_tool_name
