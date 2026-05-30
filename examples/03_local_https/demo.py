"""03_local_https — ship Baton events to a local HTTP collector.

Same scenario as ``01_stdout`` and ``02_local_file``. Only the sink
changes — events go over HTTP to a local collector instead of stderr
or a file.

Run the collector in one terminal:

    python collector.py

Run the demo in another:

    python demo.py

The collector prints each event it receives. The wire contract here is
exactly what a hosted collector (see ``04_hosted_console``) consumes —
the difference is the URL.
"""

from __future__ import annotations

from baton import Client, SignalType
from baton.sinks import HttpSink


def main() -> None:
    client = Client(
        vendor_id="example-vendor",
        consent_token="ct_demo",
        sink=HttpSink(url="http://127.0.0.1:8765", api_key="dev-key"),
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
    print("Sent 7 events to http://127.0.0.1:8765/v0/events")


if __name__ == "__main__":
    main()
