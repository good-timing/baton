# Baton SDK

**Pre-1.0.** The public API is not yet stable. Breaking changes are flagged in [SPEC §13](https://github.com/good-timing/baton/blob/main/docs/SPEC.md) and [CHANGELOG.md](https://github.com/good-timing/baton/blob/main/CHANGELOG.md).

MCP standardizes how agents discover and call your tools. It does not capture *why* a call happened or whether it helped the user. Baton instruments those interactions on the vendor side, either by wrapping your MCP server or through direct library calls in your own code. It captures intent, the tool calls, expected outcomes and observed outcomes, plus friction signals, and hands each one to a sink. Correlation, policy and routing all happen downstream of the sink, not in this package.

**The docs are at [goodtiming.ai/docs.html](https://goodtiming.ai/docs.html)**: both integration paths, the proxy, the gateway and the Console, with the full configuration reference. This page is the short version.

## Install

```sh
pip install baton-sdk
```

One line for both integration paths. It installs `pydantic`, `anyio` and `httpx`, and wraps the `mcp` or `fastmcp` your server already has.

## Quickstart

```python
from mcp.server.mcpserver import MCPServer
from baton import install_baton

mcp = MCPServer("echo-server")
install_baton(mcp, dsn="https://baton_pk_...@baton.goodtiming.ai/ten_.../echo-server")

@mcp.tool()
async def your_tool(...): ...
```

On `mcp` 1.x the class is `mcp.server.fastmcp.FastMCP`; on standalone `fastmcp` it is `fastmcp.FastMCP`. `install_baton` detects which you passed and raises `TypeError`, before mutating anything, if it is neither.

Copy the DSN from **/account**. It packs four values: the collector to send to, your workspace, this server, and the key that binds them. The SDK unpacks them and builds the sink itself. Tools registered before and after the call are both captured.

**Installing changes what your server advertises.** Each tool handler is wrapped, a `<vendor>_annotate` tool is registered, the server's `instructions` are rewritten, and three intent parameters are added to every tool's schema and stripped again before your handler sees them. [What gets injected](https://goodtiming.ai/docs.html#injected-tools).

**If you distribute your server, put the DSN in your source.** A stdio server runs on your user's machine, spawned by their MCP client, which passes it a fixed allowlist of environment variables (six names on macOS and Linux) plus whatever that user wrote in their own client config. Nothing from your `.env` is in either list. For a hosted server, set `BATON_DSN` and call `install_baton(mcp)` with no arguments.

Sending to your own collector instead, or trying the package before you have a key, means naming the parts rather than passing a DSN: [Without a DSN](https://goodtiming.ai/docs.html#without-dsn).

## Library API

For vendors whose customers reach the API through agent-generated code (the Skills pattern) rather than MCP tool calls. Same event envelope, same sinks, different emission boundary:

```python
from baton import Client

client = Client(dsn="https://baton_pk_...@baton.goodtiming.ai/ten_.../your-vendor")

with client.trace(
    tool_name="chat.completions.create",
    intent="summarize the user's question",
    expected_outcome="2-3 sentence answer",
) as trace:
    response = vendor_client.chat.completions.create(...)
    trace.observed(response)
```

`AsyncClient` is the async twin. Worked end to end in [`examples/skill_demo/`](https://github.com/good-timing/baton/tree/main/examples/skill_demo).

## PII scrubbing

**On by default.** `baton.scrub.Scrubber` walks every event payload and redacts email / `Bearer …` / `sk-*` / `AKIA*` / JWT / Luhn-validated card / NA-format phone, plus a list of sensitive field names. Opt out by passing a config instead of a bare DSN:

```python
from baton import install_baton, VendorConfig
from baton.scrub import identity_scrub

install_baton(mcp, VendorConfig(
    dsn="https://baton_pk_...@baton.goodtiming.ai/ten_.../echo-server",
    scrubber=identity_scrub,
))
```

**It is pattern matching, not a guarantee.** `{"name": "Jane Doe", "address": "12 Elm St"}` passes through untouched, and a card number is redacted when its digits are contiguous but **not** when they are spaced or hyphenated. Decide what your server puts in tool params and results on that basis. Do not tell your users "PII is scrubbed" on the strength of it. [What it does and does not catch](https://goodtiming.ai/docs.html#pii).

## Turning capture off

`BATON_DISABLED=1` in the environment of the process running the server, and the SDK installs nothing at all: no wrapped tools, no annotation tool, no instructions rewrite, no collector connection, no background thread.

The switch belongs to whoever RUNS the server: your user for a server you distribute, you for one you host. **If you pass this on to your users, say where the variable goes.** For a server started by an MCP client it belongs in that client's config file, **not in their shell**, which never reaches the process. [The long version](https://goodtiming.ai/docs.html#off-switch).

## More

| | |
|---|---|
| [goodtiming.ai/docs.html](https://goodtiming.ai/docs.html) | The docs: both integration paths, sinks, `VendorConfig`, the proxy, the gateway, the Console |
| [`docs/SPEC.md`](https://github.com/good-timing/baton/blob/main/docs/SPEC.md) | The wire protocol. The contract a collector consumes |
| [`docs/CHARTER.md`](https://github.com/good-timing/baton/blob/main/docs/CHARTER.md) | Why the SDK is thin, and what deliberately lives downstream of the sink |
| [`examples/`](https://github.com/good-timing/baton/tree/main/examples) | The four-rung sink ladder. Start with `examples/01_stdout/demo.py`, which needs no key |
| [`CONTRIBUTING.md`](https://github.com/good-timing/baton/blob/main/CONTRIBUTING.md) | Dev setup, the `make` targets, PR conventions |
| [`SECURITY.md`](https://github.com/good-timing/baton/blob/main/SECURITY.md) | Disclosure policy |

Apache-2.0. There is a [TypeScript SDK](https://www.npmjs.com/package/@goodtiming/baton-sdk) too, which takes the same DSN and emits the same events.

For design-partner conversations: reach out via [Good Timing](https://goodtiming.ai).
