# Baton SDK

**Pre-1.0** — the public API is not yet stable, and breaking changes are flagged in [SPEC §13](https://github.com/good-timing/baton/blob/main/docs/SPEC.md) and [CHANGELOG.md](https://github.com/good-timing/baton/blob/main/CHANGELOG.md).

## What Baton is

MCP standardizes how agents discover and call your tools. It does not capture *why* a call happened or whether it helped the user. Baton instruments those interactions on the vendor side — by wrapping your MCP server, or by direct library calls in your own code — and captures intent, the tool calls, expected outcomes and observed outcomes, plus friction signals (eight types, [SPEC §3.1](https://github.com/good-timing/baton/blob/main/docs/SPEC.md)). It hands each one to a sink. Everything after that — correlation, policy, routing — happens downstream of the sink, not in this package.

Full picture, both integration paths, and the Console: **[goodtiming.ai/docs.html](https://goodtiming.ai/docs.html)**.

## Install

```sh
pip install baton-sdk
```

One line for both integration paths. It installs `pydantic`, `anyio` and `httpx`, and wraps the `mcp` or `fastmcp` your server already has.

## Quickstart — one string

```python
from mcp.server.mcpserver import MCPServer
from baton import install_baton

mcp = MCPServer("echo-server")
install_baton(mcp, dsn="https://baton_pk_...@baton.goodtiming.ai/ten_.../echo-server")

@mcp.tool()
async def your_tool(...): ...
```

On `mcp` 1.x the class is `mcp.server.fastmcp.FastMCP`.

That is the whole configuration. Copy the string from **/account**, where it is labelled DSN. It packs four values — the collector to send to, your workspace, this server, and the key that binds them — and the SDK unpacks them and builds the sink itself.

| Resolved from the one string | From |
|---|---|
| `vendor_id` | the server segment of the DSN |
| `tenant_id` | the `ten_…` workspace segment |
| sink | an `HttpSink` pointed at the DSN's host, authenticated with its key |
| display name | your MCP server's own name, verbatim; the DSN's server segment when the server has none or it is too long |
| annotation tool | your MCP server's name, lowercased and hyphenated — `MCPServer("Acme Knowledge Base")` registers `acme-knowledge-base_annotate` — or `<server segment>_annotate` under the same conditions. Pin it with `VendorConfig(annotation_tool_name=...)` |

Tools registered before `install_baton` and tools registered after it are both captured, so the call can go anywhere in your server's setup. Top-level `install_baton` routes to whichever adapter your server needs — official `mcp` (`mcp>=1.20,<3`) or standalone `fastmcp` (`fastmcp>=2.14,<5`) — detecting on the object you pass, and raises `TypeError`, before mutating anything, if it is neither. `baton.integrations.official` and `baton.integrations.standalone` stay importable if you would rather name one.

**If you distribute your server, put the DSN in your source.** A stdio server runs on your user's machine, spawned by their MCP client, which passes it a fixed allowlist of environment variables — six names on macOS and Linux — plus whatever that user wrote in their own client config. Nothing from your `.env` is in either list.

For a hosted server, where the process starts from your own environment, set `BATON_DSN` and pass nothing:

```python
install_baton(mcp)   # reads BATON_DSN
```

An explicit `dsn=` wins over `BATON_DSN`, and a DSN decides where events are filed — collector, workspace and server — whatever `BATON_*` variables an earlier install left in the environment.

## Without a DSN

Two cases do not have one: you are sending to **your own collector** rather than a hosted one, or you are trying the package out before you have a key. Name the parts instead — `vendor_id` and a sink you construct:

```python
import os

from baton.integrations.official import install_baton, VendorConfig   # or .standalone
from baton.sinks import HttpSink

install_baton(mcp, VendorConfig(
    vendor_id="your-vendor",
    vendor_display_name="Your Vendor",
    sink=HttpSink(url=os.environ["BATON_INGEST_URL"], api_key=os.environ["BATON_API_KEY"]),
))
```

`VendorConfig` is keyword-only. The full field list, the two adapters' differences, and the sink options are in the [SDK docs](https://goodtiming.ai/docs.html#vendorconfig).

## Turning capture off

`BATON_DISABLED=1` in the environment of the process running the server, and the SDK **installs nothing at all**: no wrapped tools, no annotation tool, no instructions rewrite, no collector connection, no background thread. It is read once at startup, it never writes to stdout (the JSON-RPC stream under stdio transport), and it cannot make your server fail to boot — a config the SDK would otherwise refuse is accepted and ignored while the switch is on.

The switch belongs to whoever RUNS the server. For a server you distribute, that is your user. For one you host, it is you.

**If you pass this on to your users, say where the variable goes.** For a server started by an MCP client it belongs in the `env` block for your server in that client's config file — **not in their shell**, which never reaches the process. Someone who follows "set the environment variable" otherwise gets a server that is still capturing and believes it is not. The long version, including the allowlist and what to tell your own users, is in the [docs](https://goodtiming.ai/docs.html#off-switch).

## PII scrubbing

**On by default** (`src/baton/scrub.py`). `baton.scrub.Scrubber` walks every event payload and redacts email / `Bearer …` / `sk-*` / `AKIA*` / JWT / Luhn-validated card / NA-format phone, plus the field names `email`, `phone`, `ssn`, `api_key`, `token`, `secret`, `password`, `user_name`. Same ruleset as `baton-proxy`. Opt out with `VendorConfig(scrubber=baton.scrub.identity_scrub)`.

**It is pattern matching, not a guarantee.** `{"name": "Jane Doe", "address": "12 Elm St"}` passes through untouched. A card number is redacted when its digits are contiguous (`4111111111111111`) and **not** when they are spaced or hyphenated (`4111 1111 1111 1111`). `Bearer` values need 16+ characters to match. Decide what your server puts in tool params and results on that basis — do not tell your users "PII is scrubbed" on the strength of it.

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

`AsyncClient` is the async twin. Worked end to end in [`examples/skill_demo/`](https://github.com/good-timing/baton/tree/main/examples/skill_demo); the full surface — `client.annotate(...)`, `trace.annotate(...)`, the exception path — is in `src/baton/client.py` docstrings and [`examples/library_api_smoke_test/`](https://github.com/good-timing/baton/tree/main/examples/library_api_smoke_test).

## Sinks

`StdoutSink()` (the default — JSON Lines to stderr), `FileSink(path)`, `HttpSink(url, api_key=...)` (bounded buffer, retry, circuit breaker; built for you from a DSN) and `MultiSink([...])`. The four-rung ladder in [`examples/`](https://github.com/good-timing/baton/tree/main/examples) runs the same demo against each one — start with `python examples/01_stdout/demo.py`, which needs no key and no extras.

## Docs and contracts

| | |
|---|---|
| [goodtiming.ai/docs.html](https://goodtiming.ai/docs.html) | The docs — both integration paths, the proxy, the gateway, the Console |
| [`docs/SPEC.md`](https://github.com/good-timing/baton/blob/main/docs/SPEC.md) | The wire protocol. The contract a collector consumes |
| [`docs/CHARTER.md`](https://github.com/good-timing/baton/blob/main/docs/CHARTER.md) | Why the SDK is thin, and what deliberately lives downstream of the sink |
| [`CONTRIBUTING.md`](https://github.com/good-timing/baton/blob/main/CONTRIBUTING.md) | Dev setup, the `make` targets, PR conventions |
| [`SECURITY.md`](https://github.com/good-timing/baton/blob/main/SECURITY.md) | Disclosure policy |

Apache-2.0. The Console (ingest, worker, Channels, UI) lives in a separate repo.

For design-partner conversations: reach out via [Good Timing](https://goodtiming.ai).
