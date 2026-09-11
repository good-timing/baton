"""Suite-wide fixtures.

**No ambient ``BATON_*`` environment variable reaches a test that did not ask
for one.** The prefix is ours, so the fixture scrubs the whole namespace rather
than a list — a variable added to the SDK later is covered without anybody
remembering to edit this file, which is how the list got out of date the first
time.

Two reasons it exists, one per variable that has already caused trouble:

- ``BATON_DISABLED`` turns capture off, and a developer who has it exported
  would otherwise watch a large part of this suite fail with "no events
  arrived" and no visible explanation — the line saying why is at INFO, where
  pytest shows nothing by default. Measured: with this fixture removed and
  ``BATON_DISABLED=1`` exported, most of the suite fails — around two hundred
  tests, though the exact figure moves with which tests a machine skips, so do
  not treat it as a fixed number.
- ``BATON_DSN`` is the WIDER exposure, and it was not scrubbed until the DSN
  parser's leaks were fixed. An ambient DSN supplies the vendor id, the tenant
  id AND the sink — so a developer with one exported for a real server had this
  suite building ``HttpSink``s at a live collector and POSTing fixture events
  into a real workspace. A failing test is a nuisance; that is test data in
  production.

⚠ **Scrubbing only those two was not enough, and the docstring said otherwise.**
Found by review of the commit that added ``BATON_DSN``: five more were still
getting through. Measured — with ``BATON_VENDOR_ID``, ``BATON_TENANT_ID``,
``BATON_CONSENT_TOKEN`` and ``BATON_INTENT_PARAM`` exported, **five tests
failed**, two of them in the DSN lane's own parity file, and
``test_vendor_id_required`` did not raise at all. The claim in this docstring
is what made the gap worth finding: a guarantee stated in bold is one somebody
will rely on.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_baton_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse and suite-wide. Tests that WANT a variable set it themselves
    with ``monkeypatch.setenv`` afterwards, which wins — this runs first."""
    for name in [name for name in os.environ if name.startswith("BATON_")]:
        monkeypatch.delenv(name, raising=False)
