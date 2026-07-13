- **Context Packs: two more Parent-pack situation tools** ✅ — the Parent pack's
  situation-tool registry grows from the lone school-run tool to the three the
  pack always promised, each a thin, read-only composition of a shipped primitive:
  - **`pack_the_bag`** — a one-step-at-a-time get-ready checklist for the kids'
    next events. Composes the task-paralysis initiation lever (`decompose_task`)
    over the parent's imminent `child` commitments (the next few within a 36-hour
    horizon, far enough ahead to pack the night before), breaking "pack the bag and
    get ready for <event>" into a tiny first step plus the remaining prep steps, so
    the morning scramble becomes an ordered list. `fyi`/placeholder events are
    excluded. Event-specific with a model, deterministic-heuristic without.
  - **`sick_day`** — a sick-day replan for a day upended by a kid home sick.
    Composes the panic triage (`build_panic`, model-free) for the single calm first
    step and the count of what's genuinely pressing, plus a hardness split of
    today's own attendable commitments into **must_cover** (firm obligations you
    still have to attend or line up cover for) and **can_reschedule** (soft blocks
    you can drop to be home) — so the reshaped day is legible from the notification.

  To let a tool reach an LLM lever, the `SituationTool` handler contract gained an
  optional keyword `client` (`handler(store, *, client=None)`), resolved and passed
  through by the `packs` router; deterministic tools (the school-run leave-by)
  ignore it. Both new tools are surfaced through the same `GET`/`POST
  /packs/situations[/{tool}]` seam, gated on the Parent pack being enabled. Covered
  by `tests/test_situations.py`.
