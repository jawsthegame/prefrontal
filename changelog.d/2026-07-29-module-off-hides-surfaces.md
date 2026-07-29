- **A module you turn off now disappears from the UI too** ✅ — the pull half of
  "off means off" (the nudge half silenced every fire path). A module-owned read
  surface whose module is off — deployment-wide or via the user's Settings ▸
  Features toggle — now returns its normal *empty* shape plus a `module_off` marker:
  `GET /self-care`, `/self-care/review`, `/focus`, `/outings`, `/impulses/parked`
  and `/trips`. Because the payload keeps the shape a client already renders for
  "nothing here", an **older** app degrades to an empty card instead of breaking,
  while an updated one names the switch to flip. Writes that exist only to feed the
  intervention are refused with a 409 that names both switches (`POST
  /webhooks/focus/start`, `/webhooks/outing/start`, `POST /self-care`,
  `/self-care/mark`) via a shared `require_module` helper. **Nothing is deleted** —
  flipping the module back on restores the surface exactly as it was. Clients
  follow: the web dashboard hides the self-care and outing cards, Settings and
  `/trips` explain which switch is off (matching how `/care` handles a disabled
  pack), iOS shows a new `ModuleOffCard` on Trips and Parked impulses plus a
  pointer on the Me self-care card, and the Lock Screen self-care ring reads
  **off** rather than a misleading `0`. Offline **local notifications** clear on the
  next refresh too, since they're reconciled from these same payloads — so a
  disabled module goes quiet even off-tailnet.
- Deliberately left open, so "off" never loses data or blocks help: capturing an
  impulse (it writes a real todo), `POST /emotion/support` (it screens for crisis
  language first — a 409 there is not acceptable), passive trip detection on
  `/webhooks/location` (it carries vacation mode's auto-lift on return home),
  closing an already-open outing, and the whole **projects** feature (its module is
  only a staleness nudge, not the feature's engine). Covered by
  `tests/test_module_off_surfaces.py`.
