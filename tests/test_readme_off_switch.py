"""The README's off-switch section, pinned against the code it describes.

``tests/test_optout.py`` proves the switch BEHAVES: set it and nothing is
installed, nothing is emitted, nothing reaches stdout. Nothing proved that the
README still describes that switch correctly, and the README is the PyPI page —
the surface a stranger reads before they have the package.

The failure this exists for is silent in both directions. Rename ``_SWITCH``
and every behavioural test still passes while the published page names a
variable that does nothing; reword the section and the code is untouched while
the instruction stops working. Neither shows up in a diff review of the other
file.

⚠ **What this file does NOT cover.** ``website/docs.html`` carries a second
copy of the same section, in a different repo with no test harness of its own;
by decision (2026-09-11) the off switch lives on both surfaces, and only this
one is checked. A change to the switch still needs a hand edit there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from baton import _optout

_README = Path(__file__).resolve().parent.parent / "README.md"
_HEADING = "## Turning capture off"


def _off_switch_section() -> str:
    """The README's off-switch section, or a failure that names the rename.

    Sliced heading-to-heading rather than by line number: a section that moves
    is fine, a section that vanishes is the finding.
    """
    text = _README.read_text(encoding="utf-8")
    if _HEADING not in text:
        pytest.fail(
            f"README.md has no {_HEADING!r} section. It is the PyPI page, and the "
            "off switch is the one thing a vendor must be able to hand to their "
            "own users — if the heading was renamed, rename it here too rather "
            "than deleting this test."
        )
    after = text.split(_HEADING, 1)[1]
    return after.split("\n## ", 1)[0]


def test_the_readme_names_the_variable_the_code_actually_reads() -> None:
    """The variable comes from ``_optout``, never typed into this test.

    Typing ``BATON_DISABLED`` here would pin the README to a string this file
    asserts rather than to the one the SDK reads, and a rename would leave both
    agreeing with each other and disagreeing with the product.
    """
    section = _off_switch_section()
    switch = _optout._SWITCH
    assert switch in section, (
        f"README's off-switch section never names {switch!r}, the variable "
        f"baton._optout reads. A reader following it sets something that does "
        f"nothing.\n\nSection:\n{section}"
    )

    named = set(re.findall(r"\bBATON_[A-Z_]+\b", section))
    assert named == {switch}, (
        f"README's off-switch section names {sorted(named)} but the only "
        f"variable that switches capture off is {switch!r}. A second BATON_* "
        "name in this section reads as a second way to opt out."
    )


def test_the_value_the_readme_documents_really_disables_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the README's own instruction through the resolver.

    The section's assignment is extracted and executed rather than eyeballed,
    so a doc that says ``=0`` (or the code flipping which values mean off)
    fails here instead of in a stranger's server.
    """
    section = _off_switch_section()
    assignments = re.findall(rf"{re.escape(_optout._SWITCH)}=(\S+?)`", section)
    assert assignments, (
        f"README's off-switch section names {_optout._SWITCH} but never shows a "
        "value to set it to. The instruction a reader copies is the assignment, "
        "not the variable name."
    )

    for value in assignments:
        monkeypatch.setenv(_optout._SWITCH, value)
        assert _optout.capture_disabled() == _optout._SWITCH, (
            f"README documents {_optout._SWITCH}={value}, and capture_disabled() "
            f"does not treat that as off. _OFF_VALUES is {sorted(_optout._OFF_VALUES)}."
        )


def test_the_readme_sends_the_variable_to_the_client_config_not_a_shell() -> None:
    """The distinction that makes the instruction work at all.

    An MCP client spawns a stdio server with a short allowlist of environment
    variables, and ``BATON_DISABLED`` is not on it — so a reader who exports it
    in their shell gets a server that is still capturing and believes it is
    not. The section must send them to the client's own config.
    """
    section = _off_switch_section()
    assert "`env`" in section and "config file" in section, (
        "README's off-switch section no longer tells the reader the variable "
        "goes in the `env` block of their MCP client's config file. That is the "
        "half of the instruction that makes it work; without it the reader "
        "exports it in a shell the server never sees."
    )
    assert f"export {_optout._SWITCH}" not in section, (
        f"README's off-switch section shows `export {_optout._SWITCH}`. A shell "
        "export never reaches a server the MCP client spawns."
    )


def test_the_readme_claims_no_partial_install() -> None:
    """The promise the behavioural suite backs.

    ``test_optout.py`` proves the whole-install claim; this pins the README to
    still be making it, because a weaker sentence ("stops sending events")
    would describe a product we do not ship and would leave a vendor unable to
    tell their users what the switch does.
    """
    section = _off_switch_section()
    assert "installs nothing" in section, (
        "README's off-switch section no longer claims the SDK installs nothing "
        "at all. test_optout.py proves that claim — a weaker one understates "
        "what a vendor can promise their own users."
    )
