# FastMCP integration

Wrap an existing [FastMCP](https://github.com/jlowin/fastmcp) server with Baton in
three lines. This is the runnable MCP integration example — [`server.py`](server.py)
is an ordinary bookmarks server; the only Baton-specific code is the
`install_baton(...)` block near the bottom.

## What it shows

- **`install_baton(mcp, VendorConfig(...))`** — the whole integration. Registers
  the middleware (emits `tool_call_start` / `tool_call_end` / `tool_call_error`)
  and one sentence of server instructions. `VendorConfig(report_mode="on")`
  also registers the vendor-namespaced annotation tool, which agents use to
  report problems, and the instructions that tell them when.
- **Intent-param injection**: Baton adds a `user_goal` parameter to every tool's
  schema and strips it before your handler runs, capturing *why* a call
  happened and what success should look like, as
  `<message number>: <what the call is for>; success is <what it should show>`. This works
  even on clients that ignore server instructions (e.g. Claude Desktop), where
  the annotation tool alone would capture nothing. Toggle with
  `VendorConfig(intent_param_mode=...)`: `required` (default) | `optional`.
  `required` lists it as required in the advertised schema but never
  refuses a call that omits it: your handler still runs, and the event carries
  no `call_intent`.
- **Zero-config sink** — the example uses `StdoutSink()`, which writes one JSON
  envelope per line to stderr. No backend, no dependencies beyond the SDK. Swap in
  `HttpSink(...)` to ship to a Console (see [`../04_hosted_console/`](../04_hosted_console/)).

## Run it

```sh
pip install "baton-sdk[fastmcp]"
python examples/fastmcp_server/demo.py
```

The demo drives the wrapped server with two tool calls and prints the captured
events to stderr. You'll see:

- `tool_call_start` / `tool_call_end` events. The start event carries the *why*
  as `call_intent` (`intent_source: "injected_param"`), harvested from the
  injected param, and `params` holding exactly the arguments your tool
  received (`user_goal` stripped out).

## The integration, in full

```python
from baton.integrations.standalone import VendorConfig, install_baton
from baton.sinks import StdoutSink  # or FileSink / HttpSink / MultiSink

install_baton(
    mcp,
    VendorConfig(
        vendor_id="bookmarks",
        vendor_display_name="Bookmarks",
        sink=StdoutSink(),
    ),
)
```

`baton-sdk` depends on `pydantic`, `anyio` and `httpx`, and uses the `fastmcp`
your server already has.
