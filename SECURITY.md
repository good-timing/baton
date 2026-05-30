# Security Policy

Baton is a structured-signal capture SDK for agent-mediated tool use. It runs inside vendor MCP servers and agent code paths that handle potentially sensitive end-user data (prompts, intent strings, tool params, error bodies). Security disclosures are taken seriously.

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x (initial public release line; pre-1.0 = no API stability promise) | ✅ |

## Reporting a vulnerability

**Please report security issues privately. Do not open a public GitHub issue.**

Email: **security@goodtiming.ai**

We aim to:
- **Acknowledge receipt within 3 business days.**
- **Provide an initial assessment within 7 business days.**
- **Coordinate a fix and disclosure timeline** appropriate to the severity, typically within **90 days** of confirmed receipt.

When reporting, please include:
- A description of the vulnerability and its impact.
- The affected version(s) (or commit SHA).
- A minimal reproduction (proof-of-concept code, MCP server config, network capture, etc., as applicable).
- Any known mitigations.
- Your preferred attribution for the eventual security advisory (or "anonymous" if you prefer no credit).

## Scope

In scope:
- The `baton` package source under `src/baton/`.
- The MCP integration under `src/baton/integrations/mcp/`.
- The wire-format event envelope (SPEC §11.4) — anything that could allow tampering, forgery, or replay of events.
- PII / consent handling — any path that could leak unscrubbed end-user data, drop a consent token, or accept events without one.
- Authentication / API-key handling — anything that could leak the vendor's bearer token to other tenants, log it, or transmit it insecurely.

Out of scope:
- Vulnerabilities in vendor MCP server code (those are the vendor's responsibility — please report to the vendor directly).
- Vulnerabilities in upstream dependencies that don't materially affect Baton's behavior (please report to the dependency maintainers; we'll track via Dependabot).
- The Console worker (lives in a separate repository).
- Theoretical attacks requiring an attacker to already have arbitrary-code-execution inside the vendor's MCP server process.

## What we consider a vulnerability

- A way to make Baton ship events without a valid `consent_token` (SPEC §2.3, §3.1).
- A way to make Baton emit events from one tenant under another tenant's `tenant_id`.
- A way to make Baton's PII scrubber produce unscrubbed output for content that the configured scrub rules should redact.
- A way to leak a vendor's `api_key` to logs, stderr, or any non-target HTTPS endpoint.
- A way to make Baton's local event buffer leak events to disk or other processes.
- Any RCE, deserialization-attack, or path-traversal in the SDK's event-handling code paths.
- Supply-chain risk: any maintainer-account-takeover, tampered release, or compromised dependency that ships through `pip install baton-sdk`.

## What we do NOT consider a vulnerability

- Reports requiring physical access to the developer's machine.
- Reports requiring an attacker to already control the vendor's MCP server config (e.g., "I can change the `ingest_url`!").
- Reports that the SDK doesn't enforce a feature it documents as out-of-scope for the SDK (e.g., signing — explicitly deferred per SPEC §0 / CHARTER §8).
- DoS via excessive event volume — the SDK ships a bounded buffer with documented drop policy (SPEC §11.2, CHARTER §7 OD-7); intentional overflow is expected behavior, not a vulnerability.

Thank you for helping keep Baton and its users safe.
