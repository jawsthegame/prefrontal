- **Auto mode — a delegated todo that does the legwork and asks what it can't
  look up** ✅ (roadmap M4). The `agent` handler does *one* generation and never
  looks anything up; the new **`auto` handler** runs a **bounded research loop**
  first (`prefrontal/autorun.py`): plan → allowlisted MCP tool call → observe →
  repeat, then hands its findings to the existing `generate_prep` so the brief,
  drafts, and action items come back in the shape every surface already renders.
  One JSON turn at a time, so it satisfies the existing `Generator` protocol and
  works with either provider today. Budgets are **hard** (step cap, wall-clock
  deadline, per-observation character cap) and exhausting one is a normal outcome
  that keeps its partial findings; every step is persisted to a new
  `todo_delegations.steps` column, so a run that dies at step 4 is inspectable
  rather than vanished.
- **Two gates, not one.** A tool is callable unattended only if it's on its
  server's `allowed_tools` (the existing confirm-gated allowlist) **and** on the
  new per-server `unattended_tools` — "the user may call this after previewing it"
  is a weaker claim than "this may fire while the user is away." Empty by default
  and intersected with `allowed_tools`, so an existing deployment gains **zero**
  new autonomy and auto mode with nothing declared degrades, honestly, to today's
  prep. Google Drive et al. need no integration code: point
  `PREFRONTAL_MCP_SERVERS` at a server and name the tool.
- **Asking is a first-class move, not an error path.** "Should I get a HELOC for
  the kitchen remodel?" can't be finished by research alone — it needs your
  equity, your rate, your risk tolerance. So a run may `ask` (capped, each
  question carrying a one-line *why*), park at the new **`needs_input`** status
  with its partial write-up already on the todo, and pick the work back up when
  the answers land. Answering **re-runs** the loop with the Q&A as context rather
  than resuming a suspended one — nothing to expire, and a re-run is free to take
  a different path than the blind alley it stopped in. Answers can arrive minutes
  or days later: inline on the dashboard card (`POST
  /todos/{id}/delegate/answers`, positional so you can answer two of five now),
  from the CLI (`prefrontal todo answer <id> "…"`), or in prose via the NL
  assistant. Answered questions survive the re-run and are never re-asked.
  `needs_input` is deliberately **not** parked — the ball is with you. Covered by
  `tests/test_autorun.py`.
- **Delegation honours `ANTHROPIC_AGENTS`** ✅ — the todos router read the raw local
  client, so delegation prep (and an `auto` run's tool loop) ignored the per-agent
  provider config entirely: `provider.client("summarizer")` had exactly one call
  site in the codebase, and the *same* hand-off ran on Claude from the NL box but
  on the local model from the dashboard button and the CLI. Now resolved through
  the provider with the longer-timeout local client as the fallback. Three fixes
  this exposed: `generate_prep` caught only `OllamaError` (a cloud failure escaped
  as "prep failed unexpectedly" instead of degrading to the heuristic) and now
  catches `ProviderError` *and logs it*; the Anthropic client accepts a per-call
  `max_tokens` (the 1024 default is sized for the assistant's action lists, not a
  brief); and a reply truncated at `max_tokens` **before any text** now raises
  instead of returning `""` — on a reasoning model the thinking block is spent from
  the same budget, so claude-sonnet-5 returned a thinking-only response and the
  prep silently served its offline heuristic with no way to see why.
- **Local model: thinking off by default** ✅ — `OllamaClient` gained a `think` flag
  (`OLLAMA_THINK`, default off) that reaches the wire, so a hybrid-thinking model
  (Qwen3, DeepSeek-R1) can be the configured local model without its reasoning pass
  tripling every call. Measured on an auto-run loop turn with `qwen3:14b`: 64.5s
  with thinking (2.7 KB of reasoning) vs 25.8s without, same JSON quality — and the
  snappy inference paths run on a 10s timeout, so leaving it on would time every
  one of them out to a heuristic. An older server that rejects the field gets one
  retry without it.
