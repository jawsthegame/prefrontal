# Scriptable widget

`prefrontal-widget.js` is an iOS **home-screen and Lock Screen** widget
([Scriptable](https://scriptable.app)) showing a glanceable "right now": any
active outing with its escalation level, your next commitments today,
conflict/todo counts, and the most recent nudge Prefrontal sent (from the last
8 hours), so a push you missed is still visible. It reads the Prefrontal API
over Tailscale and opens the family view (`/family`) when tapped.

One script drives every size — it detects which family iOS is rendering:

- **Home Screen** (Small / Medium / Large): the full card — header, active
  outing, commitment list, a counts footer, and the most recent nudge (Medium /
  Large).
- **Lock Screen** (iOS 16+ accessory slots around the clock):
  - **Rectangular** — active outing (or next commitment, else the last nudge)
    plus a compact counts line.
  - **Circular** — one glyph + number (elapsed minutes / next time / open todos).
  - **Inline** — a single line beside the clock (the single most urgent thing).

  Lock Screen widgets are rendered monochrome by iOS, so those variants use SF
  Symbols + text rather than the dashboard's colors.

**Install**

1. Install Scriptable, open it, tap **+**, paste in `prefrontal-widget.js`.
2. Set `TOKEN` to your Prefrontal token (stays only on the phone).
   `BASE_URL` is already the mini's Tailscale name.
3. Run once in-app to preview (shows the Medium card). Then add it where you want:
   - **Home Screen** — long-press the home screen → add a **Scriptable** widget
     (Medium recommended) → pick this script.
   - **Lock Screen** — edit the Lock Screen → tap a widget slot → **Scriptable** →
     pick this script (choose the circular, rectangular, or inline slot).

Refreshes every ~15 min (iOS controls exact timing). Every family renders from
the same script and token.

**Which token?** On a single-user deploy, `TOKEN` is the `PREFRONTAL_WEBHOOK_SECRET`.
On a multi-user deploy each person uses **their own per-user token** (the operator
issues one per user). The widget sends it in the `X-Prefrontal-Token` header exactly
as before — only the value differs — and the server scopes every endpoint to that
token's user, so the widget shows **your** outings, commitments, and todos and never
anyone else's. Each person installs the same script on their own phone with their own
token. If the operator rotates your token (`/admin/users/<handle>/rotate`), the widget
stops loading until you paste the new one in — same for the dashboard and Shortcuts.
See [`docs/multi-tenant.md`](../../docs/multi-tenant.md) for the full model.
