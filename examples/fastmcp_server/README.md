# FastMCP integration

Wrap an existing [FastMCP](https://github.com/jlowin/fastmcp) server with Baton in
three lines. This is the runnable MCP integration example — [`server.py`](server.py)
is an ordinary bookmarks server; the only Baton-specific code is the
`install_baton(...)` block near the bottom.

## What it shows

- **`install_baton(mcp, VendorConfig(...))`** — the whole integration. Registers
  the middleware (emits `tool_call_start` / `tool_call_end` / `tool_call_error`),
  the vendor-namespaced annotation tool, and the server instructions.
- **Intent-param injection** — Baton adds an optional `baton_intent` parameter to
  every tool's schema and strips it before your handler runs, capturing *why* a
  call happened. This works even on clients that ignore server instructions (e.g.
  Claude Desktop), where the annotation tool alone would capture nothing. Toggle
  with `VendorConfig(intent_param_mode=...)`: `optional` (default) | `required` |
  `off`.
- **Zero-config sink** — the example uses `StdoutSink()`, which writes one JSON
  envelope per line to stderr. No backend, no dependencies beyond the SDK. Swap in
  `HttpSink(...)` to ship to a Console (see [`../04_hosted_console/`](../04_hosted_console/)).

## Run it

```sh
pip install baton-sdk[fastmcp]
python examples/fastmcp_server/demo.py
```

The demo drives the wrapped server with two tool calls and prints the captured
events to stderr. You'll see:

- an `annotation` event with `intent_source: "injected_param"` — the *why*,
  harvested from the injected param;
- `tool_call_start` / `tool_call_end` events — the *what*, with `call_intent` on
  the start event and `params` holding exactly the arguments your tool received
  (`baton_intent` stripped out).

## The integration, in full

```python
from baton.integrations.fastmcp import VendorConfig, install_baton
from baton.sinks import StdoutSink  # or FileSink / HttpSink / MultiSink

install_baton(
    mcp,
    VendorConfig(
        vendor_id="bookmarks",
        vendor_display_name="Bookmarks",
        consent_token=os.environ["BATON_CONSENT_TOKEN"],
        sink=StdoutSink(),
    ),
)
```

`baton-sdk`'s only base dependency is `pydantic`; `httpx` (for `HttpSink`) is the
optional `[http]` extra. Both are already required by `fastmcp`, so adding Baton
to a FastMCP server pulls in no new transitive dependencies.
