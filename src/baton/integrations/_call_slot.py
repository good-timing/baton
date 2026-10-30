"""The hand-off between the two ``tools/call`` seams.

The outer seam, on the low-level request handler, strips Baton's params and
opens a slot. The inner capture (the official adapter's ``Tool.run`` wrap, the
standalone middleware) claims it. A call whose slot nobody claimed never reached
the inner capture, so the outer seam reports it. Either way a call yields one
pair of events.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from baton.integrations._llm_text import Goals


@dataclass
class CallSlot:
    goals: Goals | None
    """``user_goal`` and the retired names, when the outer seam stripped them."""
    claimed: bool = False


current_slot: ContextVar[CallSlot | None] = ContextVar("baton_call_slot", default=None)


def claim_call() -> Goals | None:
    """Mark the open call as captured by the inner seam, and return the goals
    the outer seam stripped, or ``None`` when the inner capture strips itself.

    A slot already claimed is not this call's: a tool that calls another tool
    from inside its handler must not take its caller's goal. Not matched by
    name, because a server may route a call to a tool of another name, and
    that is still this request."""
    slot = current_slot.get()
    if slot is None or slot.claimed:
        return None
    slot.claimed = True
    return slot.goals
