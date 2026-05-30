"""02_local_file — emit Baton signals to a JSONL file.

The same scenario as ``01_stdout/demo.py``. Only the sink changes — events
go to ``./events.jsonl`` instead of stderr.

    python demo.py
    cat events.jsonl | jq .

Useful when you want to capture a session for later analysis without
standing up any infrastructure.
"""

from __future__ import annotations

from baton import Client, SignalType
from baton.sinks import FileSink


def main() -> None:
    client = Client(
        vendor_id="example-vendor",
        consent_token="ct_demo",
        sink=FileSink("./events.jsonl"),
    )

    with client.trace(
        tool_name="search_orders",
        intent="find the user's last order",
        expected_outcome="one order record",
    ) as trace:
        result = {"order_id": "o_123", "status": "shipped"}
        trace.observed(result)

    try:
        with client.trace(
            tool_name="refund_order",
            intent="refund order o_123",
            expected_outcome="refund confirmation",
        ):
            raise RuntimeError("order is past the 30-day refund window")
    except RuntimeError:
        pass

    client.annotate(
        signal_type=SignalType.DEAD_END,
        suggested_improvement=(
            "surface refund-window expiry in search_orders result so the "
            "agent doesn't recommend a refund it can't fulfill"
        ),
    )

    client.close()
    print("Wrote events to ./events.jsonl")


if __name__ == "__main__":
    main()
