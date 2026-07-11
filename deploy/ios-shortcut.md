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

> **⚠️ Shortcut name must match the deep link.** The Hyperfocus alignment-check
> Pushover (from `hyperfocus-check.workflow.json`) carries a **"Wrap up (End
> focus)"** action link that runs `shortcuts://run-shortcut?name=End%20focus`.
> That name has to be the **exact** name of your end-a-focus shortcut (here,
> **End focus**), URL-encoding spaces as `%20`. If your shortcut is named
> differently, either rename it to `End focus` or edit the `url` in the
> workflow's Pushover node to match — otherwise tapping the link does nothing.

---

## Shortcut: "Add Todo" (capture an open loop)

The plainest capture gesture: get a task out of your head and onto the list. You
supply only the **title** — Prefrontal fills in the rest (estimate, priority,
energy, and any deadline it can read out of the wording) so the todo lands
schedulable and honestly sortable without you fiddling with fields. Put it on the
Lock Screen / Back Tap / Share Sheet so it's a one-tap, one-line gesture.

1. New shortcut named **Add Todo**.
2. **Ask for Input** (Text): "What's the todo?" Phrase deadlines naturally in the
   text — "call the dentist tomorrow", "file taxes by Friday", "renew passport
   next week" — and the server parses the due date out for you.
3. **Get Contents of URL**
   - **URL:** `http://<your-mac>:8000/todos`
   - **Method:** `POST`, headers as above (token + `Content-Type: application/json`)
   - **Request Body (JSON):** `{ "title": "Provided Input" }` — replace the value
     with the **Provided Input** variable.
4. **Confirm back.** The response carries the resolved `estimate_minutes`,
   `priority`/`energy`, any parsed `deadline`, and an `augmented` map (which fields
   were inferred vs stated) — read one back so the tap that captures the task
   confirms it landed:
   - **Get Dictionary Value** `estimate_minutes` from the response → **Show
     Notification** (or **Speak Text**): *"Added — ~{estimate_minutes} min."*
   Breakdown into a tiny first step is **not** done at capture (a fresh todo
   returns `decomposition: null`) — it's help for a *stall*, offered on demand via
   `POST /todos/{id}/decompose` (or by the opt-in avoided-task sweep). If you want
   a first step now, add a follow-up **Get Contents of URL** to that endpoint and
   read back `decomposition.first_step`.
5. **Roaming, like the outing/panic shortcuts:** every tap crosses Tailscale from
   your phone. Wrap **Get Contents of URL** in an **If** that checks it succeeded;
   on failure **Show Notification** "Couldn't reach Prefrontal — tap again when
   you're back on the tailnet." (`/health` in Safari is the quick check.)

> **Want to set a field yourself?** Add a **Choose from Menu** before the POST —
> e.g. "Quick / Normal / Someday" — and send `priority` per branch (`3` / omit /
> `0`), or an explicit `estimate_minutes`, in the JSON body:
> `{ "title": "Provided Input", "priority": 0 }`. Anything you send is kept as-is;
> anything you omit is inferred. Keep the default one-field, though — letting the
> server infer is the whole point of a zero-friction capture.

> Quick test from the Mac first:
> ```sh
> curl -s -X POST http://localhost:8000/todos \
>   -H "X-Prefrontal-Token: $PREFRONTAL_WEBHOOK_SECRET" -H "Content-Type: application/json" \
>   -d '{"title":"draft the Q3 report by Friday"}' | python3 -m json.tool
> ```
> A big task like this comes back with an inferred `deadline`, an
> `estimate_minutes`, and the `augmented` sourcing (`decomposition` is `null` at
> creation — break it down later via `POST /todos/{id}/decompose`).

## Shortcut: "Capture" (Impulsivity — capture-and-defer)

The one-tap "park this and get back to it" gesture. When an impulse pulls at
your attention mid-task, capturing it relieves the *fear of forgetting* — the
thing that actually drives the switch — so you don't have to act on it now. The
impulse lands as a normal todo (flagged `source: "impulse"`), so it flows into
fitting, the briefing, and avoidance like any other open loop.

1. New shortcut named **Capture** (add it to the Lock Screen / Back Tap so it's
   reachable in one gesture — the whole point is near-zero friction).
2. **Ask for Input** (Text): "What's pulling at you?"
3. **Get Contents of URL**
   - **URL:** `http://<your-mac>:8000/webhooks/impulse/capture`
   - **Method:** `POST`, headers as above (token + `Content-Type: application/json`)
   - **Request Body (JSON):** `{ "impulse_text": "Provided Input" }` — replace
     the value with the **Provided Input** variable.
4. **Confirm back.** **Get Dictionary Value** for `confirmation` → **Show
   Notification**. The server returns a clean, speakable line — e.g. *"Parked
   "reorganize the reference folder" — it's safe in your list. Back to what you
   were doing."* It titles the raw text for you (local model, with a plain-words
   fallback) and keeps your exact words in the todo's notes.

> Pairs with the "Switching?" reflective-pause flow below; on its own it's
> already the fastest way to get a distraction out of your head and off your
> plate without opening anything.

## Shortcut: "Switching?" (Impulsivity — the reflective pause)

The friction tap. When you feel the pull to abandon what you're doing for a
shinier thing, this re-surfaces the current task, holds for a beat, then lets
you choose — so the switch is a *decision*, not a reflex. Requires an active
focus block (declare one with the Hyperfocus "Start focus" shortcut →
`/webhooks/focus/start`); if none is running, use **Capture** above instead.

1. New shortcut named **Switching?**.
2. **Get Contents of URL** → `POST http://<your-mac>:8000/webhooks/focus/switch`,
   the usual token + JSON headers, body `{}` (fires against the active block).
   - On a `409` (no active block) → **Show Notification** "No focus block
     running — use Capture instead," and stop.
3. **Get Dictionary Value** `message` → **Show Notification** (or **Speak Text**):
   e.g. *"Hold on — you're 20 min into "writing the Q3 report." Does the new
   thing actually need you now, or can it wait?"*
4. **Get Dictionary Value** `pause_seconds` → **Wait** that many seconds. (The
   pause is delivered client-side; the server only advises the length — it's
   longer early in a block, when you've most to lose.)
5. **Choose from Menu**: **Return / Capture & defer / Switch anyway**, then
   `POST /webhooks/focus/resolve` with the matching `action`
   (`return`/`defer`/`switch`). For **Capture & defer**, add an **Ask for Input**
   and send `{ "action": "defer", "impulse_text": "Provided Input" }`.
6. **Confirm back.** **Get Dictionary Value** `confirmation` → **Show
   Notification** — e.g. *"Parked "reorganize the font folder" for later —
   staying on "writing the Q3 report.""*

> Splitting the impulse (step 2) from the resolution (step 5) into two requests
> is deliberate: the impulse is counted the moment you tap, so a connection
> dropped mid-pause can't strand the block, and the honor-vs-defer ratio it
> records is what later teaches Prefrontal your switching pattern.

## Shortcut: "Trip retro" (close a trip's whole retrospective in one tap)

When Prefrontal auto-detects a round trip it later asks you to label it — and the
ambient ask carries one-tap 🏠/🧒/🙋 domain buttons for a quick file. This
Shortcut is the *fuller* version: it labels the trip, files a life-domain, **and**
captures a one-line "how it went" reflection in a single interaction, so the whole
retrospective closes from the notification without opening the dashboard. It posts
once to `/webhooks/trip/retro` (the combined endpoint) instead of chaining three
calls.

1. New shortcut named **Trip retro**.
2. **Ask for Input** (Text): "What was that trip?" → the **label**.
3. **Choose from Menu** — "Home / Kids / Personal / Shop / Work / Skip" → the
   **domain** (set a `Domain` variable per branch; leave it unset on **Skip**).
4. **Ask for Input** (Text): "How did it go? (optional)" → the **reflection**.
5. **Get Contents of URL**
   - **URL:** `http://<your-mac>:8000/webhooks/trip/retro`
   - **Method:** `POST`, headers as above (token + `Content-Type: application/json`)
   - **Request Body (JSON):**
     ```json
     { "label": "Provided Input", "domain": "Domain", "reflection": "Provided Input" }
     ```
     Replace each value with the matching variable. **Omit `trip_id`** and the
     server targets the most recent trip still awaiting a label — so you don't have
     to carry the id from the notification. (Every field is optional; send only what
     you captured. To act on a *specific* trip, include `"trip_id": <id>`.)
6. **Confirm back.** **Get Dictionary Value** `confirmation` → **Show Notification**
   (or **Speak Text**) — e.g. *"Filed “Target run” under home. Logged that it
   didn't go well."* The reflection is classified into an outcome that resolves the
   trip's episode (so honest self-report becomes learning signal); pass an explicit
   `"outcome"` (`success`/`partial`/`miss`) if you'd rather set it yourself.

> The three underlying endpoints (`/webhooks/trip/label`, `/webhooks/trip/domain`,
> `/webhooks/trip/reflect`) still exist for the one-tap domain buttons and partial
> updates; `/webhooks/trip/retro` just bundles them so a single Shortcut closes
> everything. Roaming caveat as above — wrap **Get Contents of URL** in an **If**
> and show a "couldn't reach Prefrontal" note on failure.

## Shortcut: "Panic" (overwhelmed → one first step)

The one-tap "I'm buried and don't know where to start" gesture. It asks
Prefrontal what's *actually* on fire right now — across calendar, todos, and
mail — and reads back the single first step to break the freeze. Put it on the
Lock Screen / Back Tap / Apple Watch so it's reachable the instant the fog hits.

1. New shortcut named **Panic** (😮‍💨).
2. **Get Contents of URL**
   - **URL:** `http://<your-mac>:8000/panic`
   - **Method:** `GET`
   - **Headers:** `X-Prefrontal-Token` = `<your token>`
3. **Get Dictionary Value** → key `headline` from the response.
4. **Show Notification** (or **Speak Text**) with that value. The server returns
   one ready-made, speakable line — e.g. *"5 things need you right now. Start
   here: Stop what you're doing and move toward the door right now — grab keys,
   phone, wallet, and go."* — so you don't assemble anything in Shortcuts. When
   the plate is clear it reassures instead: *"Nothing's actually on fire right
   now — take a breath."*

> **Want the whole board, not just the headline?** Skip step 3 and instead
> **Get Dictionary Value** `text` (the full Markdown triage: already-behind /
> bearing-down-soon / piling-up), then **Quick Look** it. The `headline` is the
> zero-friction default; `text` is the "show me everything" variant. There's
> also `first_step` and `counts` in the same response if you want to build a
> richer card.

> **Roaming, like the outing shortcuts:** every tap crosses Tailscale from your
> phone. Wrap **Get Contents of URL** in an **If** that checks it succeeded; on
> failure **Show Notification** "Couldn't reach Prefrontal — tap again when
> you're back on the tailnet." (`/health` in Safari is the quick check.)

The escalating *proactive* nudge — a push when you tip into overwhelm without
tapping anything — is sent by the `panic-check` n8n workflow polling
`POST /webhooks/panic/check`; you don't need a shortcut for that.

## Shortcut: "Set Alarm" (morning-prep alarm button)

The evening "early start tomorrow" push (the Time Blindness `morning_prep` nudge)
carries a one-tap **⏰ Set alarm** button. Unlike the outcome shortcuts above it
doesn't POST anything — it's a client-side `view` action that opens this Shortcut
via `shortcuts://run-shortcut?name=Set%20Alarm&input=text&text=<HH:MM>`, passing a
suggested wake time (your leave-by minus `morning_routine_minutes`) as the
Shortcut's input. So this Shortcut just needs to turn that text into an alarm:

1. New shortcut named **Set Alarm** (⏰). The name must match `alarm_shortcut_name`
   (default `Set Alarm`); rename both together if you change it.
2. It receives the time as **Shortcut Input** (text, e.g. `06:15`).
3. Add **Create Alarm** (Clock actions):
   - **Time:** set from the Shortcut Input. If Create Alarm won't take text
     directly, first **Get Numbers from Input** / split on `:` into hour and
     minute, then feed those in.
   - **Enabled:** on. (Some iOS versions expose **Toggle Alarm** instead — create
     it once and toggle, or use **Open Clock** as a fallback so you set it
     manually in one tap.)
4. (Optional) **Show Notification** → "Alarm set for <input> ⏰" for feedback.

> **No Shortcut yet?** The button still opens the Shortcuts app — it just won't
> find one to run, so the push is otherwise a normal reminder. The feature
> degrades to a plain heads-up until you add the Shortcut. To turn the button off
> entirely, clear `alarm_shortcut_name`.

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

### Automation: "Leaving Home" (did you actually leave on time?)

The reminder above is the *prediction*; this captures the *outcome* — so
Prefrontal finally learns whether you leave on time, the way it already learns
from outings, todos, and mail. It's the exact mirror of the "I'm back" arrival
geofence below (Tier 1), just on the way out.

1. Shortcuts → **Automation** → **+** → **Leave** a location → choose **Home**.
2. Action **Get Contents of URL**
   - **URL:** `http://<your-mac>:8000/webhooks/departure/left`
   - **Method:** `POST`, headers as above (token + `Content-Type: application/json`)
   - **Request Body (JSON):** `{}` — the server defaults the departure time to
     now and attributes it to the commitment you're heading to. (Optionally send
     `{ "departed_at": "<ISO8601>" }` or `{ "commitment_id": 123 }` to be explicit.)
3. Turn **Run Immediately** on (no confirmation tap).
4. (Optional) **Confirm back.** **Get Dictionary Value** `confirmation` → **Show
   Notification** — e.g. *"Logged — you left for “Dentist” on time (~8 min to
   spare). Nice."* or *"…about 12 min late."*

The server figures out which commitment the departure was for (the soonest one
whose leave window you're in), scores it against the computed leave-by, and logs
it as a `departure` episode — success if you left on time, a miss if late. It's
idempotent per commitment, so a geofence that fires twice logs once, and it also
silences any pending departure nudge for that commitment (you've left). If you
weren't heading to anything, it records nothing rather than inventing an episode.

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
