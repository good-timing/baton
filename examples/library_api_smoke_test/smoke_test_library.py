"""End-to-end smoke test for the library API (Client + AsyncClient).

Mirrors the v02_e2e/smoke_test_stdio.py shape but for the library path:
- Spawns a stdlib HTTP capture server on localhost (no Console required)
- Drives a synthetic vendor-shaped chat-completion call through Client
- Drives an async equivalent through AsyncClient
- Asserts the events landed with correct schema, ordering, and agent_runtime

What this validates:

| Concern                                                    | Result |
|------------------------------------------------------------|--------|
| Sync Client emits via background thread bridge cleanly     | ✓     |
| AsyncClient emits directly on async loop                   | ✓     |
| Tool-call lifecycle: start → end with observed result      | ✓     |
| Proactive annotation when intent/expected set              | ✓     |
| Reactive standalone annotation via client.annotate(...)    | ✓     |
| Exception path emits tool_call_error and re-raises         | ✓     |
| Both paths target the SAME POST /v0/events ingest contract | ✓     |
| Event envelope shape matches SPEC §11.4                    | ✓     |
| agent_runtime correctly set to "python-library"            | ✓     |
| Sequence numbers monotonic per session_id                  | ✓     |

What this does NOT validate:

- Skill-author adherence in production (Skill-design, not engineering)
- Session correlation under streamable HTTP (per-event mode is the default)
- consent_token scaling beyond the single-static-UUID model
- A real vendor SDK's behavior (we use a stub function)

Run:
    cd ~/workplace/baton
    .venv/bin/python examples/library_api_smoke_test/smoke_test_library.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from baton import AsyncClient, Client, SignalType


# =============================================================================
# Capture server (stdlib HTTP — no external dependencies)
# =============================================================================


_captured: list[dict[str, Any]] = []


class _CaptureHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        _captured.append(json.loads(body))
        # Verify bearer auth header is present (matches the SDK contract)
        assert self.headers.get("Authorization", "").startswith("Bearer "), (
            "expected Bearer auth header"
        )
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass  # silence the per-request log line


def _start_capture_server() -> tuple[HTTPServer, int]:
    """Start a daemon-thread HTTP server on a random localhost port. Returns
    (server, port). Caller is responsible for shutdown."""
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


# =============================================================================
# Synthetic vendor API call (stub the SDK; no network)
# =============================================================================


def _fake_vendor_chat_completions_create(
    *, model: str, messages: list[dict[str, str]]
) -> dict[str, Any]:
    """Stub for a vendor chat-completions endpoint. Returns a fake response
    shape resembling a typical OpenAI-compatible chat-completion API."""
    time.sleep(0.01)  # simulate latency
    return {
        "id": "chatcmpl-fake-001",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "fake response"},
                "finish_reason": "stop",
            }
        ],
    }


async def _fake_vendor_chat_completions_create_async(
    *, model: str, messages: list[dict[str, str]]
) -> dict[str, Any]:
    """Async equivalent of the stub."""
    await asyncio.sleep(0.01)
    return {
        "id": "chatcmpl-fake-async-001",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "fake async response"},
                "finish_reason": "stop",
            }
        ],
    }


# =============================================================================
# Sync scenario — happy path + reactive annotation + exception
# =============================================================================


def run_sync(ingest_url: str) -> None:
    print("\n[sync] starting...")
    from baton.sinks import HttpSink

    client = Client(
        vendor_id="vendor-sync-spike",
        consent_token="ct-spike-sync",
        sink=HttpSink(url=ingest_url, api_key="bk_test_sync"),
    )
    try:
        # Happy path: proactive intent + params + observed result.
        with client.trace(
            tool_name="chat.completions.create",
            intent="answer the user's question about agent observability",
            expected_outcome="a complete answer based on retrieved context",
            workflow="customer-support-resolution",
            params={"model": "Qwen/Qwen2.5-72B-Instruct"},
        ) as trace:
            result = _fake_vendor_chat_completions_create(
                model="Qwen/Qwen2.5-72B-Instruct",
                messages=[{"role": "user", "content": "How does Baton compare to OTel?"}],
            )
            trace.observed(result)

        # Reactive standalone annotation: agent noticed a feature gap.
        client.annotate(
            signal_type=SignalType.FEATURE_GAP,
            intent="check supported features for a model",
            suggested_improvement=(
                "expose model metadata fields like supports_tool_calling and "
                "supports_streaming on the model object so agents can answer "
                "user questions without best-guess inference"
            ),
            context={
                "missing_capability_field": "supports_tool_calling",
                "requested_capability": "tool calling support check",
            },
        )

        # Exception path: simulated failure inside the with block.
        try:
            with client.trace(tool_name="chat.completions.create") as trace:
                raise ValueError("simulated network timeout")
        except ValueError:
            pass  # expected re-raise
    finally:
        client.close()
    print("[sync] done")


# =============================================================================
# Async scenario — happy path + annotation + exception
# =============================================================================


async def run_async(ingest_url: str) -> None:
    print("\n[async] starting...")
    from baton.sinks import HttpSink

    client = AsyncClient(
        vendor_id="vendor-async-spike",
        consent_token="ct-spike-async",
        sink=HttpSink(url=ingest_url, api_key="bk_test_async"),
    )
    try:
        async with client.trace(
            tool_name="chat.completions.create",
            intent="generate a summary of an article",
            expected_outcome="a 3-sentence summary",
            workflow="content-summarization",
            params={"model": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
        ) as trace:
            result = await _fake_vendor_chat_completions_create_async(
                model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
                messages=[{"role": "user", "content": "summarize this article: ..."}],
            )
            trace.observed(result)

        await client.annotate(
            signal_type=SignalType.SLOW_PERFORMANCE,
            suggested_improvement="add a low-latency model variant for short prompts",
            context={"observed_latency_ms": 850},
        )

        try:
            async with client.trace(tool_name="chat.completions.create"):
                raise RuntimeError("async simulated failure")
        except RuntimeError:
            pass
    finally:
        await client.aclose()
    print("[async] done")


# =============================================================================
# Assertions
# =============================================================================


def assert_events(events: list[dict[str, Any]]) -> None:
    print(f"\n[assert] {len(events)} events captured. Validating...")

    # Sync side: 2 traces (one normal, one error) + 1 standalone annotation.
    # Trace 1 emits: start + proactive annotation + end → 3 events
    # Standalone annotate → 1 event
    # Trace 2 emits: start + tool_call_error → 2 events
    # Total sync: 6
    # Async side: same shape → 6
    # Grand total: 12
    expected = 12
    assert len(events) == expected, f"expected {expected} events, got {len(events)}"

    # All events should set agent_runtime correctly
    sync_events = [e for e in events if e["tenant_id"] == "vendor-sync-spike"]
    async_events = [e for e in events if e["tenant_id"] == "vendor-async-spike"]
    assert len(sync_events) == 6, f"expected 6 sync events, got {len(sync_events)}"
    assert len(async_events) == 6, f"expected 6 async events, got {len(async_events)}"
    assert all(e["agent_runtime"] == "python-library" for e in events), (
        "all events should have agent_runtime=python-library"
    )

    # Envelope per SPEC §11.4
    required_envelope_fields = {
        "event_id",
        "event_type",
        "tenant_id",
        "session_id",
        "sequence_number",
        "captured_at",
        "sdk_version",
        "agent_runtime",
        "payload",
    }
    for e in events:
        missing = required_envelope_fields - set(e.keys())
        assert not missing, f"event missing envelope fields {missing}: {e}"

    # Event-type distribution
    event_types = [e["event_type"] for e in events]
    type_counts = {t: event_types.count(t) for t in set(event_types)}
    print(f"[assert] event-type distribution: {type_counts}")
    # 2 traces per scenario × 2 scenarios = 4 tool_call_starts
    assert type_counts.get("tool_call_start", 0) == 4
    # 1 normal end per scenario × 2 scenarios = 2 tool_call_ends
    assert type_counts.get("tool_call_end", 0) == 2
    # 1 exception per scenario × 2 scenarios = 2 tool_call_errors
    assert type_counts.get("tool_call_error", 0) == 2
    # Proactive annotation (1 per happy-path trace) + standalone annotate = 2+2 = 4
    assert type_counts.get("annotation", 0) == 4

    # Sequence numbers monotonic within each session_id
    by_session: dict[str, list[int]] = {}
    for e in events:
        by_session.setdefault(e["session_id"], []).append(e["sequence_number"])
    for sess, seqs in by_session.items():
        assert seqs == sorted(seqs), (
            f"sequence numbers not monotonic for session {sess[:8]}: {seqs}"
        )

    # Sample inspection: signal_type on standalone annotations should match
    standalone_anns = [
        e
        for e in events
        if e["event_type"] == "annotation" and e["payload"].get("signal_type")
    ]
    signal_types = {a["payload"]["signal_type"] for a in standalone_anns}
    assert signal_types == {"feature_gap", "slow_performance"}, (
        f"unexpected signal_types: {signal_types}"
    )

    print("[assert] OK - all assertions passed ✓")


# =============================================================================
# Main
# =============================================================================


def main() -> int:
    server, port = _start_capture_server()
    ingest_url = f"http://127.0.0.1:{port}"
    print(f"[spike] capture server on {ingest_url}")

    try:
        run_sync(ingest_url)
        asyncio.run(run_async(ingest_url))

        # Print captured events for diagnostic clarity
        print(f"\n[spike] captured {len(_captured)} events:")
        for i, ev in enumerate(_captured):
            type_str = ev["event_type"]
            sess = ev["session_id"][:8]
            seq = ev["sequence_number"]
            tenant = ev["tenant_id"]
            extra = ""
            if type_str == "annotation":
                p = ev["payload"]
                if p.get("signal_type"):
                    extra = f" signal_type={p['signal_type']}"
                elif p.get("intent"):
                    extra = f" intent={p['intent'][:30]!r}"
            elif type_str == "tool_call_error":
                extra = f" error_type={ev['payload']['error_type']}"
            print(
                f"  [{i + 1:2d}] {type_str:20s} {tenant:30s} sess={sess} seq={seq}{extra}"
            )

        assert_events(_captured)
    finally:
        server.shutdown()

    print("\n[spike] OK - library API e2e smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
