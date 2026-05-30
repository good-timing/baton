"""01_stdout — emit Baton signals to stderr.

Zero config. No backend. No credentials. Run it, see exactly what the
SDK captures:

    python demo.py 2>baton.log
    # (events on stderr; demo's own prints on stdout)

The SDK is identical across all four example rungs (01 → 04). Only the
sink changes. Here the sink is ``StdoutSink``, which writes one JSON
envelope per line to stderr.
"""

from __future__ import annotations

from baton import Client, SignalType
from baton.sinks import StdoutSink


def main() -> None:
    client = Client(
        vendor_id="example-vendor",
        consent_token="ct_demo",
        sink=StdoutSink(),
    )

    # Happy path: announce intent, call the tool, record the outcome.
    with client.trace(
        tool_name="search_orders",
        intent="find the user's last order",
        expected_outcome="one order record",
    ) as trace:
        result = {"order_id": "o_123", "status": "shipped"}
        trace.observed(result)

    # Failure path: trace catches the exception and emits tool_call_error.
    try:
        with client.trace(
            tool_name="refund_order",
            intent="refund order o_123",
            expected_outcome="refund confirmation",
        ):
            raise RuntimeError("order is past the 30-day refund window")
    except RuntimeError:
        pass  # the SDK already emitted the error event; we just don't crash

    # Reactive friction signal — not tied to a trace, but informs the
    # vendor that the refund path is a dead end for this user.
    client.annotate(
        signal_type=SignalType.DEAD_END,
        suggested_improvement=(
            "surface refund-window expiry in search_orders result so the "
            "agent doesn't recommend a refund it can't fulfill"
        ),
    )

    client.close()


if __name__ == "__main__":
    main()
