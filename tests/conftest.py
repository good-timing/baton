"""Suite-wide fixtures.

Exists for one reason: **the SDK now reads an environment variable that turns
capture off**, and a developer who has it exported would otherwise watch a
large part of this suite fail with "no events arrived" and no visible
explanation — the line saying why is at INFO, where pytest shows nothing by
default.

``DO_NOT_TRACK`` is cleared too, even though the SDK deliberately no longer
reads it (reversed 2026-09-10; see ``baton._optout``). It costs one line, and
if anyone re-adds it, the tests that assert it does nothing should be what
fails — not fifty unrelated ones on the machine of whoever happens to have it
set.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _capture_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient off switch reaches a test that did not ask for one.

    Autouse and suite-wide. Tests that WANT the switch set it themselves with
    ``monkeypatch.setenv`` afterwards, which wins — this fixture runs first.
    """
    for name in ("BATON_DISABLED", "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)
