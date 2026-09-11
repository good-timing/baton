# Baton SDK

*Structured signal capture for agent-mediated tool use. Thin event capture surface with pluggable sinks (stdout / file / HTTP / fan-out); a worker on the other side of the sink assembles signals, applies policy, and dispatches.*

**Pre-1.0** — public API not yet stable; breaking changes flagged in [SPEC §13](docs/SPEC.md). Vendor integration via `install_baton(mcp, ...)` against either the official Anthropic `mcp` SDK (`baton.integrations.official`) or the standalone `fastmcp` library (`baton.integrations.standalone`); library API path (`baton.Client` / `AsyncClient`) for Skill-instrumented code. Thin SDK + fat collector worker per [CHARTER ADR-4](docs/CHARTER.md). MCP tool-call events captured across Claude Code, Cursor, and Claude Desktop; the proactive + reactive annotation flow works on Claude Code and Cursor (per-runtime support matrix in [SPEC §5.1.3](docs/SPEC.md)). See [`docs/SPEC.md`](docs/SPEC.md) for the wire protocol.

![Baton in action — events streaming to stderr](docs/demo.gif)

*30 seconds, zero config — `python examples/01_stdout/demo.py` emits structured signals you can pipe through `jq`. See [`examples/`](examples/) for the four-rung sink ladder.*

---

## What Baton is

MCP is Anthropic's [Model Context Protocol](https://modelcontextprotocol.io) — the standard way agents (Claude Code, Cursor, ChatGPT, etc.) discover and call vendor tools. MCP standardizes the transport and tool-discovery layer; it doesn't capture *why* a call happened or whether it actually helped the user. Baton instruments agent–tool interactions on the vendor side — either by wrapping a vendor's MCP server (middleware) or by direct library-API calls in vendor code — and captures the four things only an agent-using-a-tool has in one context — **intent**, **tool calls**, **observed outcomes**, **expected outcomes** — plus friction signals (eight types per [SPEC §3.1](docs/SPEC.md)). It hands these as events to a sink of your choice; an `HttpSink` pointed at a collector (your own or a hosted Console) is the production path — the collector interprets, applies policy, and routes structured signals to the vendor's agent layer (which triages, deflects, or escalates to human support).

> **PII scrubbing is on by default** (`src/baton/scrub.py`). `baton.scrub.Scrubber` recursively walks every event payload and redacts email / `Bearer …` / `sk-*` / `AKIA*` / JWT / Luhn-validated card / NA-format phone, plus the field-name overrides `email`, `phone`, `ssn`, `api_key`, `token`, `secret`, `password`, `user_name`. Same ruleset as `baton-proxy`. Opt out with `VendorConfig(scrubber=baton.scrub.identity_scrub)`.
>
> **It is pattern matching, not a guarantee, and the difference is measurable.** `{"name": "Jane Doe", "address": "12 Elm St"}` passes through untouched. A card number is redacted when its digits are contiguous (`4111111111111111`) and **not** when they are spaced or hyphenated (`4111 1111 1111 1111`). `Bearer` values need 16+ characters to match. Decide what your server puts in tool params and results on that basis — do not tell your users "PII is scrubbed" on the strength of this.

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
pip install baton-sdk                  # core only — library API for Skill-instrumented code
pip install "baton-sdk[mcp]"           # +MCP integration for the official `mcp` SDK (Anthropic's)
pip install "baton-sdk[fastmcp]"       # +MCP integration for the standalone `fastmcp` library
pip install "baton-sdk[http]"          # +HttpSink (POST events to a collector); needs httpx
pip install "baton-sdk[all]"           # everything
```

Quote the extras. `zsh` — the default shell on macOS — globs square brackets,
so an unquoted `pip install baton-sdk[mcp]` fails with `no matches found`
before pip ever runs.

Core SDK ships always. Protocol-specific surfaces live under `baton.integrations.*` and require opt-in extras — the same pattern Sentry / Datadog / OpenTelemetry use.

**Dependency footprint — near-zero for the vendor.** The core installs one runtime dependency (`pydantic`), and UUIDv7 is generated in-tree (no `uuid6`). Crucially, when you wrap an existing MCP server the *marginal* cost is **zero**: the official `mcp` package and standalone `fastmcp` already require `pydantic` and `httpx`, so `baton-sdk` (and `baton-sdk[http]` for the Console path) add nothing your server doesn't already have. The stdlib-only demo path (`StdoutSink` / `FileSink`) needs no extras at all.

## Quickstart — one string

```python
from mcp.server.fastmcp import FastMCP
from baton import install_baton

mcp = FastMCP("echo-server")
install_baton(mcp, dsn="https://baton_pk_...@ingest.goodtiming.ai/ten_<32 hex>/echo-server")

@mcp.tool()
async def your_tool(...): ...
```

That is the whole configuration. Copy the string from **/account**, where it is
labelled DSN. It packs four values — the collector to send to, your workspace,
this server, and the key that binds them — and the SDK unpacks them and builds
the sink itself. Nothing else is required: no `VendorConfig`, no `HttpSink`, no
consent token.

Tools registered before `install_baton` and tools registered after it are both
captured, so the call can go anywhere in your server's setup.

Top-level `install_baton` routes to whichever adapter your server needs,
detecting on the object you pass, and raises `TypeError` — before mutating
anything — if it is neither. `baton.integrations.official` and
`baton.integrations.standalone` stay importable if you would rather name one;
both take the same `dsn=` argument.

What the one string resolves to:

| Resolved | From |
|---|---|
| `vendor_id` and display name | the server segment of the DSN |
| `tenant_id` | the `ten_…` workspace segment |
| sink | an `HttpSink` pointed at the DSN's host, authenticated with its key |
| annotation tool | `<server>_annotate` |
| consent token | `customer-consented` (the default; override with `VendorConfig`) |

**If you distribute your server, put the DSN in your source.** A stdio server
runs on your user's machine, spawned by their MCP client, which passes it a
fixed allowlist of environment variables — six names on macOS and Linux — plus
whatever that user wrote in their own client config. Nothing from your `.env`
is in either list. See
[Turning capture off](#turning-capture-off) for the same constraint from the
other direction.

For a hosted server, where the process starts from your own environment, set
`BATON_DSN` and pass nothing:

```python
install_baton(mcp)   # reads BATON_DSN
```

An explicit `dsn=` wins over `BATON_DSN`, and both win over every other
`BATON_*` variable. That last rule matters when you re-onboard a server: the
new DSN beats the old install's leftover environment, rather than the stale
value quietly filing your events under the previous server's name.

## Full configuration — without a DSN

Two cases do not have one: you are sending to **your own collector** rather
than a hosted one, or you are trying the package out before you have a key.
Name the parts instead.

Two parallel adapters cover the two production Python MCP libraries. The vendor-facing API (`install_baton`, `VendorConfig`, `BatonHandle`) is identical across both — only the import path differs.

### Which one do I need?

| You import FastMCP via… | Use the adapter at… | Install extra |
|---|---|---|
| `from mcp.server.fastmcp import FastMCP` (Anthropic's official `mcp` SDK, `mcp>=1.20,<3`. On `mcp` 2.x the class moved: `from mcp.server.mcpserver import MCPServer` — the adapter supports both) | `baton.integrations.official` | `baton-sdk[mcp]` |
| `from fastmcp import FastMCP` (standalone `fastmcp` library by jlowin — 2.x through 4.x, `fastmcp>=2.14,<5`) | `baton.integrations.standalone` | `baton-sdk[fastmcp]` |

### Official `mcp` SDK path

```python
import os
from mcp.server.fastmcp import FastMCP
from baton.integrations.official import install_baton, VendorConfig
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
from baton.integrations.standalone import install_baton, VendorConfig
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

That's the integration. `install_baton` registers a vendor-namespaced annotation tool (named after your MCP server — a server called `"Acme Knowledge Base"` gets `acme-knowledge-base_annotate`; override with `VendorConfig(annotation_tool_name=...)`), sets the MCP server `instructions` motivating proactive + reactive annotation, captures events at the MCP transport boundary, and hands those events to your sink. The SDK is whitelabeled — no Baton-branded strings reach the calling agent or end user.

Under the hood the two adapters use different hook mechanisms — the official `mcp` SDK's FastMCP has no middleware system, so its adapter wraps each registered tool handler in place; the standalone `fastmcp` library uses its native middleware chain. The choice doesn't surface to vendors; both emit identical events through the same sink layer.

## Turning capture off

`BATON_DISABLED=1` in the environment of the process running the server, and the
SDK installs nothing at all: no wrapped tools, no annotation tool, no
instructions rewrite, no collector connection, no background thread. (A handle
or client still exposes a sink attribute — a no-op stand-in holding no key and
opening no socket — so finding one there is not a sign the switch failed.) Your server starts and
behaves exactly as it would if `install_baton` were not in the file. It is read
once at startup, it never writes to stdout (which is the JSON-RPC stream under
stdio transport), and it cannot make your server fail to boot — a config the
SDK would otherwise refuse is accepted and ignored while the switch is on.

The switch belongs to whoever RUNS the server. For a server you distribute,
that is your user. For one you host, it is you — your users cannot set an
environment variable on your machine, and there is no per-user opt-out today.

**If you pass this on to your users, say where the variable goes.** For a
server started by an MCP client, it belongs in the `env` block for your server
in that client's config file — **not in their shell**. MCP clients spawn
servers with a fixed, short allowlist of environment variables
(`mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS` — six names on macOS and Linux,
a different dozen on Windows), and `BATON_DISABLED` is on neither list, so an
exported one never reaches your process.
Someone who follows "set the environment variable" gets a server that is still
capturing and believes it is not.

What you tell your users about what is captured is yours to write, on your own
surface. The SDK does not put words in your README beyond the switch.

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

client = Client(dsn="https://baton_pk_...@ingest.goodtiming.ai/ten_<32 hex>/your-vendor")

# …or name the parts, for your own collector:
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
| Annotation surface | MCP tool (`<your-server>_annotate`) with MUST/REQUIRED framing | Python function calls (`trace.annotate(...)`) |
| Vendor API call captured? | Yes (vendor controls MCP server) | Yes (agent calls vendor API from inside trace context) |
| Where partner invests | Wire SDK into their MCP server | Author + maintain a Baton-aware Skill |
| Best fit | Vendors with MCP servers as their primary surface | Vendors using Skills as their primary distribution |

Both paths emit identical events through the same sink — downstream of the sink, correlation + policy + dispatch are unchanged.

## Development

```sh
make install          # uv sync --locked --extra dev  (uv.lock is committed)
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
│   ├── _dsn.py                      # the packed connection string, parse only
│   ├── install.py                   # top-level install_baton — routes to an adapter
│   └── integrations/
│       ├── official/    # Official `mcp` SDK adapter (tool-handler wrapping)
│       └── standalone/  # Standalone `fastmcp` adapter (native middleware chain)
├── docs/
│   ├── SPEC.md                     # the wire protocol — the hero artifact
│   └── CHARTER.md                  # load-bearing project decisions
├── examples/          # the four-rung sink ladder, plus skill_demo + library_api_smoke_test
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

Pre-1.0. Wire format and public API are not yet stable; breaking changes will be flagged in [docs/SPEC.md §13](docs/SPEC.md) and the top-level [CHANGELOG.md](CHANGELOG.md).

For design-partner conversations: reach out via [Good Timing](https://goodtiming.ai).
