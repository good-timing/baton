"""PII scrubbing per SPEC §7.

Ships a no-op identity scrubber by default; real PII rule sets (email
addresses, API-key-shaped strings, vendor-supplied custom rules) land in
a future iteration. The interface is in place so the middleware / sink
pipeline can be wired without churn when the real scrubbing lands.
"""

from __future__ import annotations

from typing import Any


def identity_scrub(value: Any) -> Any:
    """No-op scrubber.

    Callers (e.g., ``BatonMiddleware``) accept a ``scrubber`` argument so
    vendors can supply their own. The default is this identity function so
    nothing breaks while a real implementation is being built.
    """
    return value
