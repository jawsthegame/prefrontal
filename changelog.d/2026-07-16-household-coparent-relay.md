- **Household: message your co-parent** ✅ — a free-text "keep them posted" channel,
  the dictate-and-relay follow-up to the trip check-in. `POST /household/relay
  {"message": …}` sends the note verbatim (name-prefixed — "Dana: running 20 late,
  start dinner") as a push to the other co-parent's device. General and ungated —
  usable anytime, not tied to an active trip — and stateless (a plain push, nothing
  stored); a solo household is a friendly no-op. Reuses the relay seam the trip
  check-in built (`relay_to_coparents` → `deliver_to_member`/`household_notice`). iOS:
  a dictation **Update Co-Parent** App Intent (Action Button / Shortcuts) and
  `APIClient.relayUpdate` (a queued capture write, so an off-tailnet update replays on
  reconnect).
