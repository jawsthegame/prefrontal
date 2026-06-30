# Scriptable widget

`prefrontal-widget.js` is an iOS home-screen widget ([Scriptable](https://scriptable.app))
showing a glanceable "right now": any active outing with its escalation level,
your next commitments today, and conflict/todo counts. It reads the Prefrontal
API over Tailscale and opens the family view (`/family`) when tapped.

**Install**

1. Install Scriptable, open it, tap **+**, paste in `prefrontal-widget.js`.
2. Set `TOKEN` to your `PREFRONTAL_WEBHOOK_SECRET` (stays only on the phone).
   `BASE_URL` is already the mini's Tailscale name.
3. Run once in-app to preview. Then long-press the home screen → add a
   **Scriptable** widget (Medium recommended) → pick this script.

Refreshes every ~15 min (iOS controls exact timing). Small/Medium/Large all
render; Medium shows the most useful detail.
