# Baton SDK

*Structured signal capture for agent-mediated tool use. Thin event capture surface with pluggable sinks (stdout / file / HTTP / fan-out); a worker on the other side of the sink assembles signals, applies policy, and dispatches.*

**Pre-1.0 (`0.2.0`)** — public API not yet stable; breaking changes flagged in [SPEC §13](docs/SPEC.md). Vendor integration via `install_baton(mcp, ...)` against either the official Anthropic `mcp` SDK (`baton.integrations.mcp`) or the standalone `fastmcp` library (`baton.integrations.fastmcp`); library API path (`baton.Client` / `AsyncClient`) for Skill-instrumented code. Thin SDK + fat collector worker per [CHARTER ADR-4](docs/CHARTER.md). MCP tool-call events captured across Claude Code, Cursor, and Claude Desktop; the proactive + reactive annotation flow works on Claude Code and Cursor (per-runtime support matrix in [SPEC §5.1.3](docs/SPEC.md)). See [`docs/SPEC.md`](docs/SPEC.md) for the wire protocol.

![Baton in action — events streaming to stderr](docs/demo.gif)

*30 seconds, zero config — `python examples/01_stdout/demo.py` emits structured signals you can pipe through `jq`. See [`examples/`](examples/) for the four-rung sink ladder.*

---

## What Baton is

MCP is Anthropic's [Model Context Protocol](https://modelcontextprotocol.io) — the standard way agents (Claude Code, Cursor, ChatGPT, etc.) discover and call vendor tools. MCP standardizes the transport and tool-discovery layer; it doesn't capture *why* a call happened or whether it actually helped the user. Baton instruments agent–tool interactions on the vendor side — either by wrapping a vendor's MCP server (middleware) or by direct library-API calls in vendor code — and captures the four things only an agent-using-a-tool has in one context — **intent**, **tool calls**, **observed outcomes**, **expected outcomes** — plus friction signals (eight types per [SPEC §3.1](docs/SPEC.md)). It hands these as events to a sink of your choice; an `HttpSink` pointed at a collector (your own or a hosted Console) is the production path — the collector interprets, applies policy, and routes structured signals to the vendor's agent layer (which triages, deflects, or escalates to human support).

> **PII scrubbing is on by default** as of the next release (`src/baton/scrub.py`). `baton.scrub.Scrubber` recursively walks every event payload and redacts email / `Bearer …` / `sk-*` / `AKIA*` / JWT / Luhn-validated CC / NA-format phone, plus the field-name overrides `email/phone/ssn/api_key/token/secret/password`. Same ruleset as `baton-proxy`. Vendors needing raw payloads can opt out with `VendorConfig(scrubber=baton.scrub.identity_scrub)`.

## Shape of the protocol — agent-to-agent, not agent-to-human

```
   customer  ↔  customer agent  ↔  Baton  ↔  vendor agent  ↔  vendor support
     ↑          (Claude / Cursor /    (this project)    (vendor's AI         (humans, last
   human         ChatGPT / Codex)                       assistant)            resort)
```

Baton is the **protocol layer connecting two agent layers**, with humans on both ends. The vendor's agent (triage / deflection / structured-action routing) is the FIRST consumer of Baton signals; humans are the fallback when the agent can't resolve. This is the shape of modern agent-to-agent support — not agent-to-human handoff.

## Implementation — two integration paths

### MCP middleware path

```
Customer agent (Claude / Cursor / ChatGPT / …)
            │ MCP transport
            ▼
   ┌────────────────────────────┐
   │  Vendor MCP server         │
   │  ┌──────────────────────┐  │      Sink
   │  │ baton-sdk            │  │  (stdout / file / http / multi)
   │  │  • middleware        │  │ ─────────────────▶ Collector
   │  │  • annotation tool   │  │                    (your own, or a hosted
   │  │  • capture surface   │  │                     Console — Baton is
   │  │  • PII scrub         │  │                     collector-agnostic)
   │  └──────────────────────┘  │                          │
   │  ┌──────────────────────┐  │                          ▼
   │  │ vendor tools         │  │                    Vendor agent layer
   │  └──────────────────────┘  │                    (triage / deflection /
   └────────────────────────────┘                     routing)
                                                            │
                                                            ▼
                                                     Human support
                                                     (last resort)
```

### Library API path (Skills pattern)

```
Customer agent runtime (Claude Code / Cursor — following a vendor-published Skill)
   ┌──────────────────────────────────────────┐
   │  agent-generated code (vendor's Skill)   │
   │  ┌────────────────────────┐              │      Sink
   │  │ baton.Client           │              │  (stdout / file / http / multi)
   │  │  • client.trace(...)   │              │ ─────────────────▶ Collector
   │  │  • client.annotate     │              │                    (same as MCP path)
   │  │  • PII scrub           │              │                          │
   │  └────────────────────────┘              │                          ▼
   │  ┌────────────────────────┐              │                    Vendor agent layer
   │  │ vendor SDK / HTTP call │ ───► Vendor  │                          │
   │  └────────────────────────┘     API      │                          ▼
   └──────────────────────────────────────────┘                    Human support
```

Everything downstream of the sink is identical across both paths — same wire envelope, same collector, same vendor-agent / human-support routing. The SDK has no state, no policy, no routing logic; everything beyond capture lives on the other side of the sink. See [docs/SPEC.md §11](docs/SPEC.md) for the capture/interpretation/egress separation.

## Install

```sh
pip install baton-sdk              # core only — library API for Skill-instrumented code
pip install baton-sdk[mcp]         # +MCP integration for the official `mcp` SDK (Anthropic's)
pip install baton-sdk[fastmcp]     # +MCP integration for the standalone `fastmcp` library
pip install baton-sdk[all]         # everything
```

Core SDK ships always. Protocol-specific surfaces live under `baton.integrations.*` and require opt-in extras — the same pattern Sentry / Datadog / OpenTelemetry use.

## Minimal MCP integration

Two parallel adapters covering the two production Python MCP libraries. The vendor-facing API (`install_baton`, `VendorConfig`, `BatonHandle`) is identical across both — only the import path differs.

### Which one do I need?

| You import FastMCP via… | Use the adapter at… | Install extra |
|---|---|---|
| `from mcp.server.fastmcp import FastMCP` (Anthropic's official `mcp` SDK — the dominant library) | `baton.integrations.mcp` | `baton-sdk[mcp]` |
| `from fastmcp import FastMCP` (standalone `fastmcp` library, v2.x by jlowin) | `baton.integrations.fastmcp` | `baton-sdk[fastmcp]` |

### Official `mcp` SDK path

```python
import os
from mcp.server.fastmcp import FastMCP
from baton.integrations.mcp import install_baton, VendorConfig
from baton.sinks import HttpSink   # or StdoutSink / FileSink / MultiSink

mcp = FastMCP("your-vendor-mcp")
install_baton(mcp, VendorConfig(
    vendor_id="your-vendor",
    vendor_display_name="Your Vendor",
    consent_token=os.environ["BATON_CONSENT_TOKEN"],
    sink=HttpSink(
        url=os.environ["BATON_INGEST_URL"],
        api_key=os.environ["BATON_API_KEY"],
    ),
))

@mcp.tool()
async def your_tool(...): ...
```

### Standalone `fastmcp` path

```python
import os
from fastmcp import FastMCP
from baton.integrations.fastmcp import install_baton, VendorConfig
from baton.sinks import HttpSink

mcp = FastMCP("your-vendor-mcp")
install_baton(mcp, VendorConfig(
    vendor_id="your-vendor",
    vendor_display_name="Your Vendor",
    consent_token=os.environ["BATON_CONSENT_TOKEN"],
    sink=HttpSink(
        url=os.environ["BATON_INGEST_URL"],
        api_key=os.environ["BATON_API_KEY"],
    ),
))

@mcp.tool()
async def your_tool(...): ...
```

That's the integration. `install_baton` registers a vendor-namespaced annotation tool (`<vendor_id>_annotate`), sets the MCP server `instructions` motivating proactive + reactive annotation, captures events at the MCP transport boundary, and hands those events to your sink. The SDK is whitelabeled — no Baton-branded strings reach the calling agent or end user.

Under the hood the two adapters use different hook mechanisms — the official `mcp` SDK's FastMCP has no middleware system, so its adapter wraps each registered tool handler in place; the standalone `fastmcp` library uses its native middleware chain. The choice doesn't surface to vendors; both emit identical events through the same sink layer.

## Sinks — where events go

The SDK is sink-agnostic. The capture surface is the same regardless of destination:

| Sink | Use case |
|---|---|
| `StdoutSink()` | Zero config, no backend. Events to stderr as JSON Lines. Default if `sink=` is omitted. |
| `FileSink("./events.jsonl")` | Capture to a file for later analysis. |
| `HttpSink(url=..., api_key=...)` | POST to any HTTP collector — your own, a hosted Console, anyone's. Bounded buffer + retry + circuit breaker. |
| `MultiSink([...])` | Fan out (e.g., stdout + http during dev). |

Four runnable examples laddered by complexity in [`examples/`](examples/) — `01_stdout` → `02_local_file` → `03_local_https` → `04_hosted_console`. The SDK is identical across all four; only the sink changes.

## Library API — for non-MCP integrations

For vendors whose customers reach the API via agent-generated code (Skills pattern) rather than MCP tool calls, the library API (`baton.Client` / `baton.AsyncClient`) is the equivalent capture surface. Same event envelope, same sink layer, same wire contract — different emission boundary:

```python
from baton import Client, SignalType
from baton.sinks import HttpSink

client = Client(
    vendor_id="your-vendor",
    consent_token=os.environ["BATON_CONSENT_TOKEN"],
    sink=HttpSink(url=..., api_key=...),
)

with client.trace(
    tool_name="chat.completions.create",
    intent="summarize the user's question",
    expected_outcome="2-3 sentence answer",
) as trace:
    response = vendor_client.chat.completions.create(...)
    trace.observed(response)
```

Worked end-to-end at [`examples/skill_demo/`](examples/skill_demo/); full surface (sync + async parity, `client.annotate(...)`, `trace.annotate(...)`, exception path) covered in `src/baton/client.py` docstrings and validated by [`examples/library_api_smoke_test/`](examples/library_api_smoke_test/).

### Choosing between MCP integration and Library API

| Concern | MCP integration (`install_baton`) | Library API (`Client`) |
|---|---|---|
| Where instrumentation lives | Vendor side (in MCP server runtime) | Agent side (in agent-generated code) |
| Setup | Vendor's MCP server adds 5 lines | Vendor publishes a Skill teaching agents the pattern |
| Reliability | Deterministic — wrap/middleware runs on every tool call | Soft — depends on agent following the Skill |
| Annotation surface | MCP tool (`<vendor>_annotate`) with MUST/REQUIRED framing | Python function calls (`trace.annotate(...)`) |
| Vendor API call captured? | Yes (vendor controls MCP server) | Yes (agent calls vendor API from inside trace context) |
| Where partner invests | Wire SDK into their MCP server | Author + maintain a Baton-aware Skill |
| Best fit | Vendors with MCP servers as their primary surface | Vendors using Skills as their primary distribution |

Both paths emit identical events through the same sink — downstream of the sink, correlation + policy + dispatch are unchanged.

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
│   ├── sinks.py                     # Sink ABC + StdoutSink / FileSink / HttpSink / MultiSink
│   ├── events.py / scrub.py / _state.py  # core substrate
│   └── integrations/
│       ├── mcp/      # Official `mcp` SDK adapter (install_baton, VendorConfig, tool-handler wrapping)
│       └── fastmcp/  # Standalone `fastmcp` library adapter (install_baton, VendorConfig, middleware)
├── docs/
│   ├── SPEC.md                     # the wire protocol — the hero artifact
│   └── CHARTER.md                  # load-bearing project decisions
├── examples/          # runnable examples (skill_demo, library_api_smoke_test)
├── tests/             # test suite
├── pyproject.toml
├── Makefile
├── AGENTS.md          # per-repo guidance for AI coding agents (agents.md convention)
├── CHANGELOG.md       # user-facing release notes (SPEC §13 has wire-format changes)
├── CONTRIBUTING.md    # dev setup + PR conventions
├── CODE_OF_CONDUCT.md
├── SECURITY.md        # disclosure policy
├── LICENSE            # Apache 2.0
└── README.md          # this file
```

The Console (ingest + worker + Channels + UI) lives in a separate sibling repo.

## Status

Pre-1.0 (`0.1.0`). Wire format and public API are not yet stable; breaking changes will be flagged in [docs/SPEC.md §13](docs/SPEC.md) and the top-level [CHANGELOG.md](CHANGELOG.md).

For design-partner conversations: reach out via [Good Timing](https://goodtiming.ai).
