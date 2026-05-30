# Baton SDK

*Structured signal capture for vendor MCP servers. Thin event emitter; the Console worker assembles signals, applies policy, and dispatches.*

**Pre-1.0 (`0.1.0`)** — public API not yet stable; breaking changes flagged in [SPEC §13](docs/SPEC.md). Wraps a vendor's MCP server; thin SDK + fat Console worker per [CHARTER OD-7](docs/CHARTER.md). Validated end-to-end against Claude Code, Cursor, and Claude Desktop. See [`docs/SPEC.md`](docs/SPEC.md) for the wire protocol.

---

## What Baton is

MCP is Anthropic's [Model Context Protocol](https://modelcontextprotocol.io) — the standard way agents (Claude Code, Cursor, ChatGPT, etc.) discover and call vendor tools. Baton wraps a vendor's MCP server and captures the four things only an agent-using-a-tool has in one context — **intent**, **tool calls**, **observed outcomes**, **expected outcomes** — plus friction signals (eight types per [SPEC §3.1](docs/SPEC.md)). It emits these as events to a Console backend that interprets, applies vendor-defined policy, and routes structured signals to the vendor's agent layer (which triages, deflects, or escalates to human support).

> **PII scrubbing in 0.1.x is a no-op identity function** (`src/baton/scrub.py`). Vendors handling sensitive end-user data should wire their own scrubber via `VendorConfig(scrubber=...)` until the default scrubber lands; events otherwise ship with whatever params/results/error bodies cross the MCP transport.

## Shape of the protocol — agent-to-agent, not agent-to-human

```
   customer  ↔  customer agent  ↔  Baton  ↔  vendor agent  ↔  vendor support
     ↑          (Claude / Cursor /    (this project)    (vendor's AI         (humans, last
   human         ChatGPT / Codex)                       assistant)            resort)
```

Baton is the **protocol layer connecting two agent layers**, with humans on both ends. The vendor's agent (triage / deflection / structured-action routing) is the FIRST consumer of Baton signals; humans are the fallback when the agent can't resolve. This is the shape of modern agent-to-agent support — not agent-to-human handoff.

## Implementation in one diagram

```
Customer agent (Claude / Cursor / ChatGPT / …)
            │ MCP transport
            ▼
   ┌────────────────────────────┐
   │  Vendor MCP server         │
   │  ┌──────────────────────┐  │   HTTPS (events)
   │  │ baton-sdk            │  │ ─────────────────▶ Good Timing Console
   │  │  • middleware        │  │                    (ingest + worker +
   │  │  • annotation tool   │  │                     policy + Channels)
   │  │  • event emitter     │  │                          │
   │  │  • PII scrub         │  │                          ▼
   │  └──────────────────────┘  │                    Vendor agent layer
   │  ┌──────────────────────┐  │                    (Pylon AI / their own
   │  │ vendor tools         │  │                     triage agent / etc.)
   │  └──────────────────────┘  │                          │
   └────────────────────────────┘                          ▼
                                                     Human support
                                                     (last resort)
```

The SDK has no state, no policy, no Channels. Everything beyond capture lives in the Console worker. See [docs/SPEC.md §11](docs/SPEC.md) for the capture/interpretation/egress separation.

## Install

```sh
pip install baton-sdk              # core only — library API for Skill-instrumented code
pip install baton-sdk[mcp]         # +MCP integration for FastMCP-wrapping vendors
pip install baton-sdk[all]         # everything
```

Core SDK ships always. Protocol-specific surfaces live under `baton.integrations.*` and require opt-in extras — the same pattern Sentry / Datadog / OpenTelemetry use. See `docs/design-notes/integration_reorg.md` for the rationale.

## Minimal MCP integration

```python
import os
from fastmcp import FastMCP
from baton.integrations.mcp import install_baton, VendorConfig

mcp = FastMCP("your-vendor-mcp")
install_baton(mcp, VendorConfig(
    vendor_id="your-vendor",
    vendor_display_name="Your Vendor",
    console_url=os.environ["BATON_CONSOLE_URL"],
    api_key=os.environ["BATON_API_KEY"],
    consent_token=os.environ["BATON_CONSENT_TOKEN"],
))

@mcp.tool()
async def your_tool(...): ...
```

That's the integration. `install_baton` registers a vendor-namespaced annotation tool (`<vendor_id>_annotate`), sets MCP server `instructions` motivating proactive + reactive annotation, installs middleware that emits events at the MCP transport boundary, and ships those events to the Console. The SDK is whitelabeled — no Baton-branded strings reach the calling agent or end user.

> **Onboarding friction is a known issue.** Today every integration needs a `console_url` + `api_key` + `consent_token` — meaning you can't try Baton without standing up a Console first. A **zero-config local playground mode** (events to local JSONL with no auth ceremony) is the next thing landing on the `0.1.x` line; see `docs/design-notes/` for the design discussion when it lands.

For vendors whose customers reach the API via agent-generated code (Skills pattern, not MCP), the library API (`baton.Client` / `baton.AsyncClient`) is the equivalent capture surface — see [`docs/SKILLS_LIBRARY_API_DRAFT.md`](docs/SKILLS_LIBRARY_API_DRAFT.md) and the worked example at [`examples/skill_demo/`](examples/skill_demo/).

## Development

```sh
make install          # uv / pip install -e ".[dev]" in .venv
make test             # pytest -q
make ci               # lint + typecheck + test (CI gate)
make format           # ruff format
```

See `Makefile` for the full target list.

## What's in this repo

```
baton/
├── src/baton/         # SDK package (Python)
│   ├── client.py                    # library API (Client, AsyncClient, Trace)
│   ├── emitter.py / events.py / scrub.py / _state.py  # core substrate
│   └── integrations/
│       └── mcp/      # MCP integration (install_baton, VendorConfig, middleware, annotation tool)
├── docs/
│   ├── SPEC.md                     # the wire protocol — the hero artifact
│   ├── CHARTER.md                  # load-bearing project decisions
│   ├── SKILLS_LIBRARY_API_DRAFT.md # library API surface design
│   └── design-notes/               # engineering memos & design-validation records
├── examples/          # runnable examples (skill_demo, library_api_smoke_test)
├── tests/             # test suite
├── pyproject.toml
├── Makefile
├── CLAUDE.md          # per-repo guidance for Claude Code sessions
├── CHANGELOG.md       # user-facing release notes (SPEC §13 has wire-format changes)
├── CONTRIBUTING.md    # dev setup + PR conventions
├── CODE_OF_CONDUCT.md
├── SECURITY.md        # disclosure policy
├── LICENSE            # Apache 2.0
└── README.md          # this file
```

The Console (ingest + worker + Channels + UI) lives in a separate sibling repo.

## Strategic context

Baton is the customer-facing product surface of the Good Timing support-agent thesis (v0.3) — the application layer on top of MCP, specifically for product-quality signal capture. The wider framing (signals, not just incidents) de-risks Anthropic absorbing structured handoff into MCP itself, because product-quality signal capture is not what MCP's retry semantics address.

## Status

Pre-1.0 (`0.1.0`). Wire format and public API are not yet stable; breaking changes will be flagged in [docs/SPEC.md §13](docs/SPEC.md) and the top-level [CHANGELOG.md](CHANGELOG.md).

For design-partner conversations: reach out via [Good Timing](https://goodtiming.ai).
