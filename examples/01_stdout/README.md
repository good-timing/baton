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

Six event envelopes — one per emission:

| Sequence | Event type | Why |
|---|---|---|
| 1 | `tool_call_start` | `search_orders` invoked |
| 2 | `annotation` | proactive: agent's intent + expected outcome |
| 3 | `tool_call_end` | `search_orders` returned |
| 4 | `tool_call_start` | `refund_order` invoked |
| 5 | `annotation` | proactive: intent for the second call |
| 6 | `tool_call_error` | `refund_order` raised |
| 7 | `annotation` | reactive: `dead_end` signal |

Each envelope is the SPEC §11.4 wire shape — identical across all four example rungs (01 → 04). Only the sink changes; the data the SDK captures does not.

## What's next

| Rung | What changes |
|---|---|
| [`02_local_file/`](../02_local_file/) | Sink → `FileSink("./events.jsonl")` |
| [`03_local_https/`](../03_local_https/) | Sink → `HttpSink("http://localhost:8000")` + a 30-line collector |
| [`04_hosted_console/`](../04_hosted_console/) | Sink → `HttpSink` pointed at a hosted Console |
