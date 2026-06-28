# iOS Shortcuts for Prefrontal

Two one-tap shortcuts for outcome logging, plus an optional location automation.
All of them POST to Prefrontal's `/webhooks/shortcut` endpoint with your token.

**Base URL:** use your Mac mini's Tailscale name or IP, e.g.
`http://mac-mini.tail-scale.ts.net:8000` (see `docs/deployment.md` step 5).

**Token:** the value of `PREFRONTAL_WEBHOOK_SECRET` from your `.env`.

---

## Shortcut: "Made it"

1. Shortcuts app → **+** → name it **Made it**.
2. Add action **Get Contents of URL**.
   - **URL:** `http://<your-mac>:8000/webhooks/shortcut`
   - **Method:** `POST`
   - **Headers:**
     - `X-Prefrontal-Token` = `<your token>`
     - `Content-Type` = `application/json`
   - **Request Body:** `JSON`
     ```json
     {
       "action": "made_it",
       "episode_type": "departure",
       "channel": "notification"
     }
     ```
3. (Optional) Add **Show Notification** → "Logged ✅" for feedback.
4. Add the shortcut to your Home Screen / Lock Screen / Apple Watch for one tap.

## Shortcut: "Missed it"

Duplicate "Made it" and change the body's `"action"` to `"missed_it"`. Everything
else stays the same.

> Want a single shortcut instead of two? Add a **Choose from Menu** action with
> "Made it" / "Missed it" / "Partial" options and set `action` per branch.

---

## Passing real numbers (optional)

If your departure reminder knows the predicted time, have the reminder shortcut
stash it and the logging shortcut include it so the memory layer can learn your
actual lag:

```json
{
  "action": "missed_it",
  "episode_type": "departure",
  "predicted_value": 15,
  "actual_value": 24,
  "context": "leaving home, weekday morning",
  "channel": "notification"
}
```

`predicted_value` / `actual_value` are minutes (any REAL number works); `context`
is free text the summarizer can later bucket on.

---

## Automation: log a departure trigger by location

1. Shortcuts → **Automation** → **+** → **Leave** a location (e.g. Home).
2. Action **Get Contents of URL**, same URL/headers as above, body:
   ```json
   { "action": "log", "episode_type": "departure", "outcome": "partial", "context": "left home (geofence)" }
   ```
3. Turn **Run Immediately** on (no confirmation tap).

This gives the system passive departure signals even when you don't tap anything.

---

## Troubleshooting

- **401 Unauthorized** — token mismatch; re-check the `X-Prefrontal-Token` header.
- **Could not connect** — phone not on the tailnet, or wrong host/port. Test the
  same URL in Safari's address bar (the `/health` path returns JSON).
- **422 Unprocessable** — body isn't valid JSON, or `action:"log"` was sent
  without an `outcome`.
