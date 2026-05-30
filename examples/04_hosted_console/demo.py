"""04_hosted_console — ship Baton events to a hosted Console.

The same scenario as the other three rungs. Only the sink configuration
changes — same ``HttpSink`` class as ``03_local_https``, pointed at a
hosted URL with a real bearer token.

Set two env vars before running:

    export BATON_INGEST_URL="https://your-vendor.console.example.com"
    export BATON_API_KEY="bk_live_..."
    python demo.py

The Console is one HTTP backend among many. If you'd rather self-host a
collector, the wire contract in ``03_local_https/collector.py`` is what
you implement.
"""

from __future__ import annotations

import os
import sys

from baton import Client, SignalType
from baton.sinks import HttpSink


def main() -> int:
    try:
        ingest_url = os.environ["BATON_INGEST_URL"]
        api_key = os.environ["BATON_API_KEY"]
    except KeyError as missing:
        print(f"error: missing env var {missing}", file=sys.stderr)
        print(
            "set BATON_INGEST_URL and BATON_API_KEY, then re-run", file=sys.stderr
        )
        return 1

    client = Client(
        vendor_id="example-vendor",
        consent_token="ct_demo",
        sink=HttpSink(url=ingest_url, api_key=api_key),
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
    print(f"Sent 7 events to {ingest_url}/v0/events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
