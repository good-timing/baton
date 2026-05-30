# Examples

Runnable examples showing how to use Baton in different integration shapes. Each subdirectory is self-contained — `cd` into it and follow its `README.md`.

## What's here

| Directory | What it shows |
|---|---|
| [`skill_demo/`](skill_demo/) | The **library API** in the Skill-instrumented agent-code pattern. Uses a stubbed vendor SDK so it runs offline. Demonstrates `client.trace(...)`, `trace.observed(...)`, `trace.annotate(...)`, the failure-with-reactive-ticket flow, and the local ingest emulator (`local_ingest.py`) that mimics the Console's `POST /v0/events` endpoint. |
| [`library_api_smoke_test/`](library_api_smoke_test/) | A self-contained end-to-end **smoke test** for the library API that runs an in-process HTTP capture server (no external Console required) and asserts the full SPEC §11.4 event envelope shape across both sync (`Client`) and async (`AsyncClient`) paths. Useful as a copyable starting point for your own integration tests. |

## What's NOT here

- A FastMCP integration example. See the `install_baton(mcp, VendorConfig(...))` pattern in [`docs/SKILLS_LIBRARY_API_DRAFT.md`](../docs/SKILLS_LIBRARY_API_DRAFT.md) and the SPEC. A full runnable MCP example is on the roadmap.
- Vendor-specific examples. Baton is vendor-agnostic by charter (see [`docs/CHARTER.md`](../docs/CHARTER.md) §4); examples use generic stubs.

## Prerequisites

All examples assume you've set up the dev environment:

```sh
make install        # creates .venv with [dev,mcp] extras
```
