# PR-wrap playbook — add Baton to a published MCP server

The go-to-market motion for small teams: open a small, friendly PR against a
published FastMCP server that adds Baton, so the maintainer can *see* the signal
their server could be capturing — intent, friction, outcomes — in 30 seconds,
with zero new dependencies and zero infra.

This folder is the reference: [`server.py`](server.py) is a stand-in for the
maintainer's server; the only change is the three-line block at the bottom.

## The diff you open

```diff
+ from baton.integrations.fastmcp import VendorConfig, install_baton
+ from baton.sinks import StdoutSink
+
+ install_baton(
+     mcp,
+     VendorConfig(
+         vendor_id="bookmarks",
+         vendor_display_name="Bookmarks",
+         consent_token=os.environ.get("BATON_CONSENT_TOKEN", "demo-local"),
+         sink=StdoutSink(),  # swap for HttpSink(...) to ship to a Console
+     ),
+ )
```

Plus one line in `pyproject.toml`:

```diff
  dependencies = [
    "fastmcp>=2.10",
+   "baton-sdk",
  ]
```

That's the whole PR.

## Why a maintainer says yes

- **Zero new dependencies.** `baton-sdk`'s runtime deps are `pydantic` (and
  `httpx` only if you use `HttpSink`) — both already required by `mcp`/`fastmcp`,
  so nothing new lands in their tree. The demo path (`StdoutSink`) is pure
  stdlib.
- **Zero infra to try it.** `StdoutSink` writes JSONL to stderr. No account, no
  endpoint, no config. They flip to `HttpSink(...)` only when they want the
  dashboard.
- **Captures intent even on Claude Desktop.** Baton injects a `baton_intent`
  param into each tool's schema and strips it before the tool runs, so the
  *why* is captured on runtimes that ignore server instructions (Desktop), not
  just Claude Code. No behavior change to their tools — the param never reaches
  their handler.
- **White-label.** Every agent-facing string uses `vendor_display_name`, not
  "Baton". Their users never see us.

## Run the demo

```sh
pip install baton-sdk[fastmcp]
python examples/pr-wrap/demo.py
```

You'll see, on stderr:

- an `annotation` event with `intent_source: "injected_param"` — the captured
  *why*, harvested from the injected param;
- `tool_call_start` / `tool_call_end` events — the *what*, with `call_intent` on
  the start event and `params` holding exactly the vendor-visible arguments
  (`baton_intent` stripped).

Point the maintainer at those lines: *"this is the product analytics your server
is currently throwing away."*

## Optional: turn injection off / require it

Injection is `optional` by default. Set `VendorConfig(intent_param_mode="off")`
to disable it (annotation-tool + instructions only), or `"required"` to make the
agent always supply intent.
