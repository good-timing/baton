# 04 — hosted Console

Same scenario as the other three rungs, shipped over HTTPS to a hosted Console (or any compatible collector). This is what a real production integration looks like.

## Run

```sh
export BATON_INGEST_URL="https://your-vendor.console.example.com"
export BATON_API_KEY="bk_live_..."
python demo.py
```

## What changed

Nothing structural. Same `HttpSink` class as [`03_local_https`](../03_local_https/), pointed at a hosted URL with a real bearer token:

```diff
- sink=HttpSink(url="http://127.0.0.1:8765", api_key="dev-key"),
+ sink=HttpSink(url=os.environ["BATON_INGEST_URL"], api_key=os.environ["BATON_API_KEY"]),
```

## Console vs self-hosted

The Console is **one HTTP backend among many**. The wire contract in [`03_local_https/collector.py`](../03_local_https/collector.py) is the same one a hosted Console implements — if you'd rather run your own collector, the SDK doesn't care. Baton is collector-agnostic.

## What about a fanout?

If you want events in two places at once (e.g., stderr for live debugging + Console for production capture):

```python
from baton.sinks import HttpSink, MultiSink, StdoutSink

sink = MultiSink([
    StdoutSink(),
    HttpSink(url=os.environ["BATON_INGEST_URL"], api_key=os.environ["BATON_API_KEY"]),
])
```

A failure in one sink doesn't prevent the others from being called.
