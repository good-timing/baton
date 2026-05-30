# Examples

Runnable examples showing how to use Baton in different integration shapes. Each subdirectory is self-contained — `cd` into it and follow its `README.md`.

## The four-rung sink ladder

The same scenario, the same SDK, the same event envelopes — only the **sink** (where events go) changes. Walk up the ladder as your needs grow.

| Rung | Sink | What it shows |
|---|---|---|
| [`01_stdout/`](01_stdout/) | `StdoutSink()` | Zero config, no backend. Events to stderr as JSON Lines. See exactly what the SDK captures. |
| [`02_local_file/`](02_local_file/) | `FileSink("./events.jsonl")` | Capture a session to a file for later analysis. |
| [`03_local_https/`](03_local_https/) | `HttpSink("http://localhost:8765", ...)` | Ship to a local collector (60 lines of stdlib `http.server`). Proves the wire contract end-to-end. |
| [`04_hosted_console/`](04_hosted_console/) | `HttpSink("https://your-console/...", ...)` | Same as rung 3, pointed at a hosted Console. |

## Other examples

| Directory | What it shows |
|---|---|
| [`skill_demo/`](skill_demo/) | The library API in the Skill-instrumented agent-code pattern. Stubbed vendor SDK; runs offline. Demonstrates `client.trace(...)`, `trace.observed(...)`, `trace.annotate(...)`, the failure-with-reactive-ticket flow, and the local ingest emulator (`local_ingest.py`). |
| [`library_api_smoke_test/`](library_api_smoke_test/) | Self-contained end-to-end smoke test for the library API. Runs an in-process HTTP capture server and asserts the full SPEC §11.4 envelope shape across both sync (`Client`) and async (`AsyncClient`) paths. Copyable starting point for your own integration tests. |

## What's NOT here

- A FastMCP integration example. See the `install_baton(mcp, VendorConfig(...))` pattern in the README and SPEC. A full runnable MCP example is on the roadmap.
- Vendor-specific examples. Baton is vendor-agnostic by charter (see [`docs/CHARTER.md`](../docs/CHARTER.md)); examples use generic stubs.

## Prerequisites

All examples assume you've set up the dev environment:

```sh
make install        # creates .venv with [dev,mcp] extras
```
