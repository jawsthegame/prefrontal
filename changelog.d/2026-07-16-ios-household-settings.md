- **iOS: household settings (co-parent opt-ins)** ✅ (follows the iOS Household
  UI + chore management) — a gear on the Household tab opens a settings screen for
  the three co-parent surfaces: the **weekly mental-load check-in** schedule
  (on/off + weekday + time, `POST /household/checkin`; the server rejects enabling
  without both, and this week's responses read back in the footer), the opt-in
  **daily delta digest** (`/household/digest`), and the opt-in **load-balance**
  view (`/household/balance`). The digest and balance toggles write through
  optimistically and revert if the write fails, so the switch never lies about the
  server. All three only matter with a second parent, so for a household of one
  they're replaced by a gentle "these light up once someone joins" note. New
  `Views/HouseholdSettings.swift` + `setCheckin`/`setDigest`/`setBalance` in
  `Networking/Endpoints.swift`. Client-only (build on a Mac); endpoints are the
  existing household router.
