- **Context Packs: care-recipient names roster** ✅ — the caregiver mirror of the
  household kids' roster, closing the `/care` follow-up. A per-user list of the
  adults you look after (an aging parent, ill partner) now drives a deterministic
  `care` classification pass: an event whose title names a care recipient (e.g.
  "Mom — cardiology") is tagged `care` offline — no model needed — so it reliably
  reaches the `/care` surface, exactly as a kid's name lands an event on the
  household sheet. Set it on the `/care` page (a new "Who you're caring for" editor),
  via `GET`/`POST /care/recipients` (caregiver-pack gated), or headless with
  `prefrontal care-recipient list|set|add|remove`. Stored per-user in
  `coaching_state` (not household — a solo caregiver isn't a co-parent) and
  normalized (trim, case-insensitive de-dupe). Wired into both calendar-sync paths
  (webhook + CLI). Covered by `tests/test_classify.py`, `tests/test_memory.py`,
  `tests/test_care_surface.py`, and `tests/test_cli.py`.
