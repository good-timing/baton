# Round 9 spike — packed annotation-tool description as a Claude Desktop workaround

*Run 2026-05-18. Result: **negative.** Claude Desktop's LLM does not translate MUST/REQUIRED framing in tool descriptions into proactive or reactive annotation calls.*

## Premise

Spike Rounds 6+7 (2026-05-13) established that Claude Desktop does not surface MCP server `instructions` to the LLM. The Nov 2026 research pass confirmed Anthropic's MCP API connector docs explicitly state *"Of the feature set of the MCP specification, only tool calls are currently supported"* — implying instructions are also dropped on Claude.ai web.

Tool descriptions, by contrast, are universally delivered via `tools/list` and reach the LLM in every runtime. External sources (5ire issue #272, merge.dev's MCP tool-description guide) described packing instruction-equivalent content into tool descriptions as a workaround pattern. Round 9 tested whether this works for Baton's annotation tool against Claude Desktop.

## Setup

Edited `src/baton/integrations/mcp/annotation.py::_DEFAULT_DESCRIPTION_TEMPLATE` to pack the same MUST/REQUIRED proactive/reactive framing that lives in `instructions.py` directly into the annotation tool's description. Final rendered length: 1551 bytes (well under Claude Code's documented 2KB tool-description cap).

Test target: an MCP-wrapped vendor server under Claude Desktop, exercising the canonical `dead_end` demo case (a tool returning a known-contradictory response shape — the kind of result a capable agent should flag).

## What was tested

Prompt sent in a fresh Claude Desktop chat (the vendor stub's hardcoded scenario):

> Find me the warmest mutual connection to <target-name> (target_user_id `<uuid>`).

The stub returns 10 mutuals with `has_more: false` AND `target_profile.mutual_connections_text: "50+ other mutual connections"` — a contradiction a capable agent should annotate as `signal_type=dead_end`.

## What happened

**The new packed description was delivered to Claude Desktop** — confirmed via Claude Desktop's MCP server log:

```
2026-05-18T22:21:03.479Z [vendor] [info] Message from server:
  {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"vendor_annotate",
   "description":"Attach structured signal for Vendor. Tells Vendor
    what the user is trying to do, what you expected, and any friction you
    observed.\n\nBEFORE invo...[2877 chars truncated]..."
```

**Claude Desktop's LLM made exactly one `tools/call`:**

```
2026-05-18T22:21:18.167Z [vendor] [info] Message from client:
  {"method":"tools/call",
   "params":{"name":"social_proximity_research",
             "arguments":{"target_user_id":"<uuid>",
                          "page_limit":3,"page_size":10}},
   "jsonrpc":"2.0","id":4}
```

**Zero calls to `vendor_annotate`** — neither proactive nor reactive.

## Secondary finding

Claude *did* notice the "50+ mutual connections" figure (it surfaced the number in its user-facing response) and *did* try to be more thorough (passed `page_limit=3` instead of the default 1) — so the contradiction was partially perceived. But that perception did not translate into the structured annotation call the description framing explicitly requires.

## Verdict

**Tool descriptions are documentation surface, not priming surface — at least for Claude Desktop.** MUST/REQUIRED markers in descriptions do not change tool-call behavior. Whatever weighting Claude Desktop's LLM gives to server `instructions` (zero, per Rounds 6+7) is not recoverable through the description channel.

## Implications

- **Tool-description-as-priming is empirically eliminated** for Claude Desktop coverage.
- **v0.2 SDK auto-detection for `dead_end`, `parameter_confusion`, `feature_gap`** is now the only Baton-side path for consumer-tier coverage. Priority upgraded from "nice to have" to "load-bearing for consumer reach."
- **MCP Agents WG conversation** has fresh empirical fuel — we tested both the documented path (server `instructions`) AND the obvious workaround (tool description) and Claude Desktop honors neither. Concrete data for any future spec-discipline discussion.

## Why we kept the SDK change anyway

Defensive infrastructure. The packed description:

- Is not harmful — duplication itself is fine per Round 5 (anti-duplication framing was what backfired, not duplication).
- Is self-documenting for any MCP client that *does* read tool descriptions as priming (Cursor does to some extent; future clients may).
- Costs 1500 bytes of LLM-prompt overhead per session — small.
- Will close the gap automatically if Anthropic changes Claude Desktop's behavior to weight tool descriptions more heavily.

Reverting would require deciding all future clients will behave like Claude Desktop, which we can't predict. Keeping is the lower-regret choice.

## Reproducing

1. Confirm `src/baton/integrations/mcp/annotation.py::_DEFAULT_DESCRIPTION_TEMPLATE` carries the MUST/REQUIRED framing (Round 9 version, 1551 bytes after templating).
2. Ensure an MCP-wrapped vendor server + Claude Desktop are wired (per the vendor's README).
3. Fully quit Claude Desktop (⌘Q), reopen, start a fresh chat.
4. Send the canonical prompt above.
5. Inspect Claude Desktop's per-server MCP log (`~/Library/Logs/Claude/mcp-server-<vendor>.log`) for `tools/call` messages addressing `<vendor>_annotate`. Expect zero.
