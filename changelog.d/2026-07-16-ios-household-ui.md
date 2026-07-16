- **iOS: shared Household sheet** ✅ — the co-parent surface, native. The app
  gains a full **Household** screen mirroring the web `/kids` dashboard's
  daily-driver surface off `GET /household/sheet`: today's shared **chores** with
  one-tap Done/undone (and a routine-completed celebration), the shared
  **shopping list** (inline add, tap-to-check-off, swipe-to-remove, clear-bought),
  **star charts** (running total + next reward, quick +1 or an award sheet that
  surfaces goals reached), upcoming kid **appointments** (+ add), the **roster**
  (kids + pets) with their reference **facts**, the opt-in **load-balance** view
  (doing + carrying shares), and a **recently-changed** catch-up feed. A caller in
  no household gets the create/join empty state (`POST /household/create`,
  `/household/invites/redeem`) instead of an error, and can mint an invite code to
  add a co-parent (`POST /household/invites`, with a `ShareLink`). Rather than a
  sixth tab (which would push iOS into the "More" overflow and bury the Me tab),
  it's reached from a compact glance embedded on **Today** — which loads only the
  light, side-effect-free reads (`/household/shopping` + `/household/chores/done`)
  so rendering Today never stamps `household_seen_at` (that would silently clear
  the delta digest) and hides itself for a non-household user. New
  `Models/Household.swift`, `Networking/Endpoints.swift` household calls, and
  `Views/Household{View,Cards,Forms}.swift`. Client-only (build on a Mac);
  endpoints are the existing household router.
