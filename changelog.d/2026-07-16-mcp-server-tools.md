- **M4: built-in MCP server exposing Prefrontal's own actions as tools** ✅ — the
  serving half of the MCP anchor (`docs/roadmap-vision.md` §4). Having built the
  *client* that calls external MCP tools, Prefrontal now *serves* a small, scoped
  set of its own capabilities as MCP tools, so the delegation agent, an external
  agent, or Prefrontal's own action gate can do the thing over the standard protocol
  without the user standing up any third-party server (local-first). New
  `prefrontal/mcp_server.py` — a tiny JSON-RPC dispatcher (`initialize` /
  `tools/list` / `tools/call`) plus a `Tool` registry, served at `POST /mcp` (new
  `mcp` router, per-user-token auth, scoped store). v1 tools:
  - **`create_event`** — create a calendar commitment (reuses `normalize_event` +
    `upsert_commitment`); the "calendar-write" action, no external calendar needed.
  - **`create_todo`** — capture a todo.
  - **`place_call`** — a *scoped* reminder call: dials the caller's **own**
    configured number (`resolve_route`'s `twilio_to`) with a spoken message via the
    existing `TwilioVoiceClient`, never an arbitrary recipient — "call me and say X",
    not an outbound dialer.
  Each tool is a thin wrapper over an operation the app already exposes — no new
  autonomy — so the safety model is the app's existing one: the caller's token
  scopes every tool to *their* data, and a tool that spends an operator-configured
  resource is marked `operator_only` (`place_call`, which uses the operator's Twilio
  account) and refused for non-operators — the same rule the `/actions` client
  surface follows. A tool-level failure returns an MCP `isError` result (HTTP 200),
  not a protocol/HTTP error, per the MCP convention. Covered by
  `tests/test_mcp_server.py` (handshake, tools/list, each tool's success + error
  paths, the operator gate, notification handling, and the HTTP surface incl. auth).
  *(Open: richer tools — real form-fill, multi-stop; stdio transport; and wiring the
  delegation agent to call these.)*
