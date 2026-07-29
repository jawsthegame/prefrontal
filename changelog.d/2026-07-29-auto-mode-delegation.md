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
