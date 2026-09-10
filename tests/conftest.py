"""Suite-wide fixtures.

Exists for one reason: **the SDK reads an environment variable that turns
capture off**, and a developer who has it exported would otherwise watch a
large part of this suite fail with "no events arrived" and no visible
explanation — the line saying why is at INFO, where pytest shows nothing by
default. Measured: with this fixture removed and ``BATON_DISABLED=1`` exported,
most of the suite fails — around two hundred tests, though the exact figure
moves with which tests a machine skips, so do not treat it as a fixed number.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _capture_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient off switch reaches a test that did not ask for one.

    Autouse and suite-wide. Tests that WANT the switch set it themselves with
    ``monkeypatch.setenv`` afterwards, which wins — this fixture runs first.
    """
    monkeypatch.delenv("BATON_DISABLED", raising=False)
