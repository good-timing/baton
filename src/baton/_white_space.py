"""Unicode ``White_Space``, exactly: the set SPEC §11.4's blank rule removes.

Not ``str.strip()`` or ``str.isspace()``: those also take U+001C to U+001F,
which Go's ``strings.TrimSpace`` keeps, so the SDKs would disagree on what is
blank.
"""

from __future__ import annotations

import re

WHITE_SPACE = (
    "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
    "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def strip_white_space(value: str) -> str:
    return value.strip(WHITE_SPACE)


def is_blank(value: str) -> bool:
    return not strip_white_space(value)


# A lone surrogate cannot be UTF-8 encoded and U+0000 is refused by Postgres
# text; either would cost a collector the whole EVENT, not the one value.
UNSENDABLE = re.compile("[\x00\ud800-\udfff]")


def usable_text(value: object, max_length: int) -> bool:
    """SPEC §11.4's rule for a vendor-chosen string sent verbatim."""
    return (
        isinstance(value, str)
        and len(value) <= max_length
        and not is_blank(value)
        and not UNSENDABLE.search(value)
    )
