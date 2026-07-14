- **Dashboard "Situations" card** ✅ — the Context Packs' situation tools (the
  Parent pack's school-run leave-by, and any future pack tool) had no in-app
  surface — only `GET/POST /packs/situations…` and a descriptive CLI listing.
  Added a self-hiding **Situations** card to `/dashboard` that lists the enabled
  packs' tools (`GET /packs/situations`) and runs one on tap
  (`POST /packs/situations/{tool}`), rendering the result inline. Pack-gated, not
  household-gated: the card is hidden entirely when the enabled packs contribute
  no tools (the common case), so it appears only for someone running a pack like
  Parent. Read-only (running a tool writes nothing). Frontend-only —
  `prefrontal/webhooks/dashboard.html`; the endpoints already ship and are covered
  by `tests/test_situations.py`. Verified end-to-end in a headless browser.
