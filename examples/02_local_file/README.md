# 02 — local file

Same scenario as [`01_stdout`](../01_stdout/), but events go to a JSON Lines file.

## Run

```sh
python demo.py
cat events.jsonl | jq .
```

## What changed

One line:

```diff
- sink=StdoutSink(),
+ sink=FileSink("./events.jsonl"),
```

Everything else — the trace flow, the event envelopes, the SPEC §11.4 wire shape — is identical. That's the point of the sink abstraction: the SDK's capture surface is the same regardless of where events go.

## When to use FileSink

- Capturing a session for later analysis (`jq`, replay into a collector, etc.)
- Air-gapped or batch-mode deployments where you ship the file later
- Debugging: tail it live with `tail -f events.jsonl | jq .`
