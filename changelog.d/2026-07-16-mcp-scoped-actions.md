- **M4: scoped agentic execution via MCP tool-calls** ✅ — the milestone's anchor
  (`docs/roadmap-vision.md` §4): *"It does the thing"* through **bounded, verifiable
  tool-calls**, never an open-ended browser/computer-use agent. The generic provider
  the delegation email-send foreshadowed — the same preview→confirm→execute gate,
  now over an arbitrary MCP tool. New:
  - `prefrontal/integrations/mcp.py` — a minimal `McpClient` over MCP's JSON-RPC /
    Streamable-HTTP transport (`list_tools` / `call_tool`), injectable httpx
    transport, errors wrapped as `McpError` (a `ProviderError`). Transport only — it
    never decides *whether* to call a tool.
  - `prefrontal/config.py` — `PREFRONTAL_MCP_SERVERS` (JSON) → `McpServerConfig`
    entries, each with a url, optional bearer `auth`, and an **`allowed_tools`
    allowlist** (the bounded-action guard — a tool not listed is refused). Empty/unset
    leaves the capability dormant (local-first, opt-in). Malformed config is skipped,
    never fatal.
  - `prefrontal/actions.py` — the confirmed-action core. `preview_action` validates
    the call (server configured, tool allowlisted *and* advertised) and returns a
    `digest` pinning the exact `{server, tool, arguments}`; `run_action` re-validates
    against the *current* config, refuses a **stale** call (digest drifted → the
    confirmed call differs from the previewed one) or a structural **blocker**, then
    calls the tool. Every run logs an inert `action` audit episode (no outcome/ack, so
    it never pollutes the learning loop). `None` digest opts out (internal callers);
    an empty string is a *provided* value that can't match, so it's refused.
  - Endpoints `GET /actions/tools`, `POST /actions/preview`, `POST /actions/run`
    (new `actions` router + schemas), **operator-only** (`require_operator`) since a
    call executes operator-configured integrations with the operator's credentials —
    a thin skin over the core; a tool/transport rejection returns `ran: false` with
    the reason (report-never-raise), a stale call 409s, a blocker 422s.
  Both native actions and this MCP provider now share the one confirm gate. Covered by
  `tests/test_mcp.py` (the client against a fake JSON-RPC server incl. isError,
  transport-failure, and SSE) and `tests/test_actions.py` (allowlist∩advertised
  listing, preview blockers, run happy-path + audit, stale/empty-digest refusal,
  not-allowlisted block, tool-error handling, and the HTTP surface incl. auth/409/422).
  *(Open remainder of this M4 item: more action types built as MCP tools — calendar-
  write, forms, scoped call; wiring a tool-call onto a delegated todo's draft;
  per-user encrypted MCP sources like SMTP; stdio-transport servers; session reuse.)*
