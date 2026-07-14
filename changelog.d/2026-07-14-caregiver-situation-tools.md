- **Context Packs: Caregiver situation tools** ✅ — the Caregiver pack now
  carries its own read-only *situation tools*, the caregiver counterpart of the
  Parent pack's now-complete set, each a thin composition of a shipped primitive
  on a caregiver pressure point (`prefrontal/packs/caregiver.py`, surfaced through
  the existing `packs` router): **care run** (`care_run`) — the departure engine
  narrowed to `care` commitments, i.e. when to leave for the care recipient's
  appointments; **paperwork** (`paperwork`) — `decompose_task` over the caregiver's
  open `admin` todos (insurance/benefits/legal), so the dreaded pile becomes a
  startable few, one tiny first step each (delegated todos excluded, capped at
  `MAX_PAPERWORK_TODOS`); and **respite** (`respite`), the pack's distinctive tool —
  `self_care_status` (which of *your own* basics you've skipped today, the checks
  this pack arms) paired with `build_panic` (the single thing that genuinely needs
  you), so caring for someone else is weighed against the honest counterweight of
  caring for yourself. All read-only and gated on the pack being enabled. Covered by
  `tests/test_situations.py`.
