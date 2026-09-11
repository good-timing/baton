# Baton SDK

*Structured signal capture for agent-mediated tool use. A thin event surface with pluggable sinks (stdout / file / HTTP / fan-out); a collector on the other side of the sink assembles signals, applies policy, and dispatches.*

**Pre-1.0** — the public API is not yet stable, and breaking changes are flagged in [SPEC §13](docs/SPEC.md) and [CHANGELOG.md](CHANGELOG.md).

![Baton in action — events streaming to stderr](docs/demo.gif)

*`python examples/01_stdout/demo.py` emits structured signals you can pipe through `jq`. See [`examples/`](examples/) for the four-rung sink ladder.*

---

## What Baton is

MCP standardizes how agents discover and call your tools. It does not capture *why* a call happened or whether it helped the user. Baton instruments those interactions on the vendor side — by wrapping your MCP server, or by direct library calls in your own code — and captures intent, the tool calls, expected outcomes and observed outcomes, plus friction signals (eight types, [SPEC §3.1](docs/SPEC.md)). It hands each one to a sink. Everything after that — correlation, policy, routing — happens downstream of the sink, not in this package.

Full picture, both integration paths, and the Console: **[goodtiming.ai/docs.html](https://goodtiming.ai/docs.html)**.

## Install

```sh
pip install baton-sdk                  # core only — library API for Skill-instrumented code
pip install "baton-sdk[mcp]"           # +MCP integration for the official `mcp` SDK (Anthropic's)
pip install "baton-sdk[fastmcp]"       # +MCP integration for the standalone `fastmcp` library
pip install "baton-sdk[http]"          # +HttpSink (POST events to a collector); needs httpx
pip install "baton-sdk[all]"           # everything
```

Quote the extras. `zsh` — the default shell on macOS — globs square brackets, so an unquoted `pip install baton-sdk[mcp]` fails with `no matches found` before pip ever runs.

The core installs one runtime dependency (`pydantic`). Wrapping an existing MCP server costs nothing on top: both the official `mcp` package and standalone `fastmcp` already require `pydantic` and `httpx`.

## Quickstart — one string

```python
from mcp.server.fastmcp import FastMCP
from baton import install_baton

mcp = FastMCP("echo-server")
install_baton(mcp, dsn="https://baton_pk_...@ingest.goodtiming.ai/ten_<32 hex>/echo-server")

@mcp.tool()
async def your_tool(...): ...
```

That is the whole configuration. Copy the string from **/account**, where it is labelled DSN. It packs four values — the collector to send to, your workspace, this server, and the key that binds them — and the SDK unpacks them and builds the sink itself.

| Resolved from the one string | From |
|---|---|
| `vendor_id` and display name | the server segment of the DSN |
| `tenant_id` | the `ten_…` workspace segment |
| sink | an `HttpSink` pointed at the DSN's host, authenticated with its key |
| annotation tool | `<server>_annotate` |
| consent token | `customer-consented` (the default; override with `VendorConfig`) |

Tools registered before `install_baton` and tools registered after it are both captured, so the call can go anywhere in your server's setup. Top-level `install_baton` routes to whichever adapter your server needs — official `mcp` (`mcp>=1.20,<3`) or standalone `fastmcp` (`fastmcp>=2.14,<5`) — detecting on the object you pass, and raises `TypeError`, before mutating anything, if it is neither. `baton.integrations.official` and `baton.integrations.standalone` stay importable if you would rather name one.

**If you distribute your server, put the DSN in your source.** A stdio server runs on your user's machine, spawned by their MCP client, which passes it a fixed allowlist of environment variables — six names on macOS and Linux — plus whatever that user wrote in their own client config. Nothing from your `.env` is in either list.

For a hosted server, where the process starts from your own environment, set `BATON_DSN` and pass nothing:

```python
install_baton(mcp)   # reads BATON_DSN
```

An explicit `dsn=` wins over `BATON_DSN`, and both win over every other `BATON_*` variable — so the DSN you pass in decides where events are filed, whatever an earlier install left in the environment.

## Without a DSN

Two cases do not have one: you are sending to **your own collector** rather than a hosted one, or you are trying the package out before you have a key. Name the parts instead — `vendor_id`, `consent_token`, and a sink you construct:

```python
from baton.integrations.official import install_baton, VendorConfig   # or .standalone
from baton.sinks import HttpSink

install_baton(mcp, VendorConfig(
    vendor_id="your-vendor",
    vendor_display_name="Your Vendor",
    consent_token=os.environ["BATON_CONSENT_TOKEN"],
    sink=HttpSink(url=os.environ["BATON_INGEST_URL"], api_key=os.environ["BATON_API_KEY"]),
))
```

`VendorConfig` is keyword-only. The full field list, the two adapters' differences, and the sink options are in the [SDK docs](https://goodtiming.ai/docs.html#sdk-overview).

## Turning capture off

`BATON_DISABLED=1` in the environment of the process running the server, and the SDK **installs nothing at all**: no wrapped tools, no annotation tool, no instructions rewrite, no collector connection, no background thread. It is read once at startup, it never writes to stdout (the JSON-RPC stream under stdio transport), and it cannot make your server fail to boot — a config the SDK would otherwise refuse is accepted and ignored while the switch is on.

The switch belongs to whoever RUNS the server. For a server you distribute, that is your user. For one you host, it is you.

**If you pass this on to your users, say where the variable goes.** For a server started by an MCP client it belongs in the `env` block for your server in that client's config file — **not in their shell**, which never reaches the process. Someone who follows "set the environment variable" otherwise gets a server that is still capturing and believes it is not. The long version, including the allowlist and what to tell your own users, is in the [docs](https://goodtiming.ai/docs.html#sdk-overview).

## PII scrubbing

**On by default** (`src/baton/scrub.py`). `baton.scrub.Scrubber` walks every event payload and redacts email / `Bearer …` / `sk-*` / `AKIA*` / JWT / Luhn-validated card / NA-format phone, plus the field names `email`, `phone`, `ssn`, `api_key`, `token`, `secret`, `password`, `user_name`. Same ruleset as `baton-proxy`. Opt out with `VendorConfig(scrubber=baton.scrub.identity_scrub)`.

**It is pattern matching, not a guarantee.** `{"name": "Jane Doe", "address": "12 Elm St"}` passes through untouched. A card number is redacted when its digits are contiguous (`4111111111111111`) and **not** when they are spaced or hyphenated (`4111 1111 1111 1111`). `Bearer` values need 16+ characters to match. Decide what your server puts in tool params and results on that basis — do not tell your users "PII is scrubbed" on the strength of it.

## Library API

For vendors whose customers reach the API through agent-generated code (the Skills pattern) rather than MCP tool calls. Same event envelope, same sinks, different emission boundary:

```python
from baton import Client

client = Client(dsn="https://baton_pk_...@ingest.goodtiming.ai/ten_<32 hex>/your-vendor")

with client.trace(
    tool_name="chat.completions.create",
    intent="summarize the user's question",
    expected_outcome="2-3 sentence answer",
) as trace:
    response = vendor_client.chat.completions.create(...)
    trace.observed(response)
```

`AsyncClient` is the async twin. Worked end to end in [`examples/skill_demo/`](examples/skill_demo/); the full surface — `client.annotate(...)`, `trace.annotate(...)`, the exception path — is in `src/baton/client.py` docstrings and [`examples/library_api_smoke_test/`](examples/library_api_smoke_test/).

## Sinks

`StdoutSink()` (the default — JSON Lines to stderr), `FileSink(path)`, `HttpSink(url, api_key=...)` (bounded buffer, retry, circuit breaker; built for you from a DSN) and `MultiSink([...])`. The four-rung ladder in [`examples/`](examples/) runs the same demo against each one.

## Docs and contracts

| | |
|---|---|
| [goodtiming.ai/docs.html](https://goodtiming.ai/docs.html) | The docs — both integration paths, the proxy, the gateway, the Console |
| [`docs/SPEC.md`](docs/SPEC.md) | The wire protocol. The contract a collector consumes |
| [`docs/CHARTER.md`](docs/CHARTER.md) | Why the SDK is thin, and what deliberately lives downstream of the sink |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup, the `make` targets, PR conventions |
| [`SECURITY.md`](SECURITY.md) | Disclosure policy |

Apache-2.0. The Console (ingest, worker, Channels, UI) lives in a separate repo.

For design-partner conversations: reach out via [Good Timing](https://goodtiming.ai).
