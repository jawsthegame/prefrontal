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

## Shortcut: "Going out" (Location-Aware Task Anchor)

Declares an intention so Prefrontal can nudge you back. Pairs with the
`coffee-shop-nudge` n8n workflow.

1. New shortcut named **Going out**.
2. (Optional) **Ask for Input** (Text): "What's the plan, and back in how long?"
3. **Get Contents of URL**
   - **URL:** `http://<your-mac>:8000/webhooks/outing/start`
   - **Method:** `POST`, headers as above (token + `Content-Type: application/json`)
   - **Request Body (JSON):**
     ```json
     { "intention": "Provided Input" }
     ```
     Replace the `"intention"` value with the **Provided Input** variable. If you
     phrase it like "getting coffee, back in 15 minutes", Prefrontal parses the
     window automatically; otherwise add `"time_window_minutes": 15`.
4. (Optional) include your home coordinates once so distance can be logged:
   `"home_lat": 37.77, "home_lon": -122.41`.
5. **Confirm back (recommended).** Add **Get Dictionary Value** → key
   `confirmation` from the URL response, then **Show Notification** with that
   value. The server returns a ready-made, speakable line — e.g.
   *"Tracking “grabbing a coffee” for ~15 min (estimated — say “back in N min” to
   set it exactly). I'll nudge you to head back."* You don't assemble the
   sentence in Shortcuts; you just show what came back. Crucially, when the
   window was **guessed** it says so (`~` + "estimated"), so a wrong inference is
   visible at the tap instead of surfacing later as a mistimed nudge — the exact
   failure that let a 9:30 "going out" slip by silently before.

> **Why this matters when you're never at the mini:** every tap crosses
> Tailscale from a roaming phone. Showing the server's `confirmation` turns a
> silent success into a confirmed one — and pairs with the failure handling
> below so a dropped connection is loud, not lost. Wrap the **Get Contents of
> URL** action in an **If** that checks it succeeded; on failure, **Show
> Notification** "Couldn't reach Prefrontal — tap again when you're back on the
> tailnet" rather than failing quietly. (`/health` in Safari is the quick
> connectivity check.)

## Shortcut: "I'm back"

Closes the active outing and logs intention-vs-actual for learning.

1. New shortcut named **I'm back**.
2. **Get Contents of URL** → `POST http://<your-mac>:8000/webhooks/outing/return`,
   same headers, body `{}` (closes the most recent active outing).
3. **Confirm back (recommended).** Same pattern as "Going out": **Get Dictionary
   Value** for `confirmation`, then **Show Notification** with it — e.g.
   *"Welcome back — out 18 min, over the 10 min you planned."* `/webhooks/focus/start`
   and `/webhooks/focus/end` return the same `confirmation` field, so the
   Hyperfocus shortcuts get an identical read-back for free.

> The escalating nudges themselves (50% push, 100% push, 150% voice call) are
> sent by the n8n workflow polling `/webhooks/outing/check` — you don't need a
> shortcut for those. These two shortcuts just open and close the outing.

---

## Shortcut: "Update location" (the simplest location source)

A single automation that tells Prefrontal where you are. It's the one source of
"where am I now" for everything location-aware: it lets the coffee-shop nudge
stop once you're home **without Home Assistant**, and it powers the departure
reminder's travel-time estimate (see below).

1. Shortcuts → **Automation** → **+**. Pick a trigger that fits how fresh you
   want the fix to be — a **Time of Day** that repeats (e.g. every hour), or
   **Arrive**/**Leave** a place, or a manual one-tap shortcut.
2. Action **Get Current Location**.
3. Action **Get Contents of URL**
   - **URL:** `http://<your-mac>:8000/webhooks/location`
   - **Method:** `POST`, headers as above (token + `Content-Type: application/json`)
   - **Request Body (JSON):**
     ```json
     { "lat": "Latitude", "lon": "Longitude" }
     ```
     Replace the `"lat"`/`"lon"` values with the **Latitude** and **Longitude**
     magic variables from the Get Current Location action. (Optionally add
     `"accuracy_m"` from **Horizontal Accuracy**.)
4. **Run Immediately** on (no confirmation tap).

Prefrontal stores only the *latest* position; `GET /location` shows what it has.
Once this is running, you can leave the coffee-shop poll's `current_lat`/
`current_lon` as `null` — the server falls back to this last-known fix.

---

## Departure reminders (leave-on-time)

The `departure-reminder` n8n workflow polls `/webhooks/departure/check`, which
works out *when to leave* for your next commitment and pushes a nudge as that
moment approaches (a 30-min heads-up, a 10-min "get ready", and a "leave now").

It estimates travel time **locally** — no maps API in the hot path — from your
last-known location (the "Update location" shortcut above) to the commitment's
coordinates, padded by your learned time bias and a prep buffer. Commitments get
those coordinates (`dest_lat`/`dest_lon`) in any of three ways, tried in order:

1. **Curated places (recommended, offline)** — teach Prefrontal your recurring
   destinations once:
   ```
   POST /places  { "name": "gym", "lat": 37.77, "lon": -122.41 }
   ```
   Any commitment whose location or title contains "gym" then resolves
   instantly, with no network call. `GET /places` lists them.
2. **Network geocoding (opt-in)** — flip the flag to let the calendar sync
   resolve free-text addresses via OpenStreetMap Nominatim:
   ```
   sqlite3 prefrontal.db "UPDATE coaching_state SET value='1' WHERE key='geocoding_enabled';"
   ```
   Results are cached (so each address is looked up once), and a bad address is
   remembered as a miss rather than retried. Off by default — see the geocoding
   note in `.env.example`. Run `POST /commitments/geocode` to backfill existing
   commitments after enabling it or adding places.
3. **Explicit coordinates** — `POST /commitments` and the calendar-sync event
   body both accept `dest_lat`/`dest_lon` directly.

Without coordinates (or without a recent location) it falls back to the
commitment's `lead_minutes`, so the reminder still fires — just less precisely.
Tune the estimate with the `travel_speed_kmh`, `travel_road_factor`,
`departure_prep_minutes`, `departure_heads_up_minutes`, and
`departure_soon_minutes` coaching-state keys.

---

## Location source (passive return & gating)

Location is **optional** — without it the anchor escalates purely on elapsed
time. Wiring it in lets Prefrontal stop nudging once you're actually home. The
"Update location" shortcut above is the easiest source; the two tiers below are
alternatives — Tier 1 is an iOS arrival geofence, Tier 2 is continuous via Home
Assistant.

### Tier 1 — iOS arrival geofence (simplest)

Uses iOS's native "when I arrive" trigger to close the outing on arrival. No
continuous tracking, no extra infrastructure.

1. Shortcuts → **Automation** → **+** → **Arrive** → choose **Home**.
2. Action **Get Contents of URL** → `POST http://<your-mac>:8000/webhooks/outing/return`,
   the usual token + JSON headers, body `{}`.
3. **Run Immediately** on (no confirmation tap).

Now walking in the door closes the active outing and logs the return — even if
you never tap "I'm back". (This calls `/return` directly; it does not need the
location-gating logic in `/check`.)

### Tier 2 — Home Assistant continuous location (full gating)

The HA companion app reports your phone's position continuously as a
`device_tracker` entity. The n8n workflow reads it each poll and passes your
current coordinates into `/webhooks/outing/check`, which then **suppresses
in-progress nudges as you get within the home radius** (not just on arrival) and
annotates each outing with `distance_m`.

1. Install the **Home Assistant companion app** on your phone and enable location
   sharing. Confirm you have a `device_tracker.<your_phone>` entity with
   `latitude`/`longitude` attributes.
2. Create a **Long-Lived Access Token** in HA (profile page).
3. In the `coffee-shop-nudge` workflow, add an **HTTP Request** node *before*
   `Check Outings`:
   - `GET http://<home-assistant>:8123/api/states/device_tracker.<your_phone>`
   - Header `Authorization: Bearer <HA long-lived token>`
4. Point the `Check Outings` body at those attributes instead of `null`:
   ```json
   {
     "current_lat": {{ $json.attributes.latitude }},
     "current_lon": {{ $json.attributes.longitude }}
   }
   ```
5. Set your home coordinates when starting an outing (the "Going out" shortcut's
   optional `home_lat`/`home_lon`), and tune the radius if needed:
   `sqlite3 prefrontal.db "UPDATE coaching_state SET value='200' WHERE key='home_radius_m';"`

With Tier 2, coming home early — at any point in the outing — quietly closes it
and cancels the pending call. Tier 1 only fires on the arrival geofence; either
is a big improvement over time-only.

---

## Troubleshooting

- **401 Unauthorized** — token mismatch; re-check the `X-Prefrontal-Token` header.
- **Could not connect** — phone not on the tailnet, or wrong host/port. Test the
  same URL in Safari's address bar (the `/health` path returns JSON).
- **422 Unprocessable** — body isn't valid JSON, or `action:"log"` was sent
  without an `outcome`.
