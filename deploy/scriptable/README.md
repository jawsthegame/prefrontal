# Scriptable widget

`prefrontal-widget.js` is an iOS home-screen widget ([Scriptable](https://scriptable.app))
showing a glanceable "right now": any active outing with its escalation level,
your next commitments today, and conflict/todo counts. It reads the Prefrontal
API over Tailscale and opens the family view (`/family`) when tapped.

**Install**

1. Install Scriptable, open it, tap **+**, paste in `prefrontal-widget.js`.
2. Set `TOKEN` to your Prefrontal token (stays only on the phone).
   `BASE_URL` is already the mini's Tailscale name.
3. Run once in-app to preview. Then long-press the home screen → add a
   **Scriptable** widget (Medium recommended) → pick this script.

Refreshes every ~15 min (iOS controls exact timing). Small/Medium/Large all
render; Medium shows the most useful detail.

**Which token?** On a single-user deploy, `TOKEN` is the `PREFRONTAL_WEBHOOK_SECRET`.
On a multi-user deploy each person uses **their own per-user token** (the operator
issues one per user). The widget sends it in the `X-Prefrontal-Token` header exactly
as before — only the value differs — and the server scopes every endpoint to that
token's user, so the widget shows **your** outings, commitments, and todos and never
anyone else's. Each person installs the same script on their own phone with their own
token. If the operator rotates your token (`/admin/users/<handle>/rotate`), the widget
stops loading until you paste the new one in — same for the dashboard and Shortcuts.
See [`docs/multi-tenant.md`](../../docs/multi-tenant.md) for the full model.
