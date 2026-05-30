# 03 — local HTTPS collector

Same scenario as [`01_stdout`](../01_stdout/), shipped over HTTP to a local collector instead of going to stderr or a file. Proves the wire contract end-to-end without needing any hosted infrastructure.

## Run

Terminal 1 — start the collector:

```sh
python collector.py
```

Terminal 2 — run the demo:

```sh
python demo.py
```

You should see seven JSON envelopes appear in Terminal 1 — one per event the SDK shipped.

For a live view of structured events:

```sh
python collector.py | jq .
```

## What changed

One line:

```diff
- sink=FileSink("./events.jsonl"),
+ sink=HttpSink(url="http://127.0.0.1:8765", api_key="dev-key"),
```

## The collector

`collector.py` is ~60 lines of stdlib `http.server` — no dependencies. It accepts `POST /v0/events` with `Authorization: Bearer ...` and prints each event to stdout. This is the **exact wire contract** a hosted Console (or any compatible collector) implements. Swap the URL in `demo.py` to point at a real backend; nothing else changes.

## When to use HttpSink

- Production: ship to a hosted Console
- Development: ship to a local collector for live inspection
- Self-hosted: ship to your own collector — Baton is collector-agnostic
