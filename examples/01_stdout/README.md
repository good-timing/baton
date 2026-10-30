# 01 — stdout

The smallest possible Baton integration. Zero config, no backend, no credentials. Events go to stderr as JSON Lines so you can see exactly what the SDK captures.

## Run

```sh
python demo.py 2>baton.log
cat baton.log | head
```

Or just watch it live:

```sh
python demo.py
```

## What you should see

Five event envelopes — one per emission:

| Sequence | Event type | Why |
|---|---|---|
| 1 | `tool_call_start` | `search_orders` invoked, carrying its intent |
| 2 | `tool_call_end` | `search_orders` returned |
| 3 | `tool_call_start` | `refund_order` invoked, carrying its intent |
| 4 | `tool_call_error` | `refund_order` raised |
| 5 | `annotation` | reactive: what went wrong with `search_orders` |

Each envelope is the SPEC §11.4 wire shape — identical across all four example rungs (01 → 04). Only the sink changes; the data the SDK captures does not.

## What's next

| Rung | What changes |
|---|---|
| [`02_local_file/`](../02_local_file/) | Sink → `FileSink("./events.jsonl")` |
| [`03_local_https/`](../03_local_https/) | Sink → `HttpSink("http://localhost:8000")` + a 30-line collector |
| [`04_hosted_console/`](../04_hosted_console/) | Sink → `HttpSink` pointed at a hosted Console |
