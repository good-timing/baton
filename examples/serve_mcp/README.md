# Serving MCP yourself, with `serve()`

A hand-written MCP server: one POST route, stateless Streamable HTTP, its own
bearer tokens. Its JSON-RPC handling lives in `dispatch`, which has no Baton
in it; each request goes through `baton.serve(message, dispatch, ...)`.
Events print to stderr as JSON Lines.

    .venv/bin/python examples/serve_mcp/server.py

In another terminal:

    call() { curl -s http://127.0.0.1:8770/mcp -H "Authorization: Bearer token-alice" -d "$1"; echo; }
    call '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
    call '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    call '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_weather","arguments":{"city":"Paris","user_goal":"1: plan a walk; success is the forecast"}}}'
    call '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"weather_annotate","arguments":{"user_goal":"1: plan a walk","what_happened":"no hourly forecast","tool_name":"get_weather"}}}'

What to look for:

- `initialize`: the server's instructions, then Baton's paragraphs.
- `tools/list`: `get_weather` with a `user_goal` param, and Baton's `weather_annotate`.
- The call: `dispatch` gets the arguments without `user_goal`; stderr shows
  `tool_call_start` with `call_intent` set, then `tool_call_end`.
- The report: answered by Baton, an `annotation` event on stderr.
