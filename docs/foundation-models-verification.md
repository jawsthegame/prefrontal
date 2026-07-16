# On-device brain-dump — device verification plan

The on-device brain-dump parse (Apple **Foundation Models**, roadmap M1) is the
**one code path CI and the simulator cannot exercise**: the system language model
isn't available on a simulator or a CI runner, so `BrainDumpParser.isAvailable`
is always `false` there and every automated test drives the *server* path instead.
`PrefrontalTests` therefore only covers the framework-free seams — the wire
mapping, `normalizedTimeWindow`, the `braindump(parse:)` body — never the model
itself.

This is the manual pass that closes that gap. Run it on a real device before
trusting a change to `Capture/BrainDumpParser.swift`, `Views/BrainDumpView.swift`,
or the `parse` wire contract. It's grounded in the exact branches those files
take today.

## Preconditions

- **Device:** a physical iPhone on **iOS 26+** with **Apple Intelligence enabled**
  and the on-device model **finished downloading** (Settings ▸ Apple Intelligence
  & Siri — if it's still downloading, `isAvailable` is `false` and you'll silently
  get the server path). The app's deployment target is iOS 26, so the simulator
  is useless here; it must be hardware.
- **Build:** app-only free signing is enough (the widget/App Group aren't needed).
  See [ios/README ▸ Run on your iPhone](../ios/README.md#run-on-your-iphone).
- **Server:** a reachable Prefrontal server with the app connected (a valid token).
  You need it for the preview round-trip and for the funnel signal below.
- **Observation, pick at least the funnel + one of the others:**
  - **Capture funnel (end-to-end, no tooling):** `GET /stats/data` →
    `capture_funnel.by_provider`. An on-device capture increments `on_device`; a
    server pass increments `anthropic`/`ollama`. This is the ground-truth signal
    that the on-device path actually ran — watch the counts move as you test.
  - **Network body (privacy claim):** put an HTTPS proxy (Proxyman/Charles) on the
    device and inspect `POST /braindump`. The on-device path must send a `parse`
    object and **no top-level `text`** (the raw thought never leaves the device);
    the server path sends `text`.
  - **Xcode console:** run from Xcode to watch for crashes/guardrail logs.
    `BrainDumpParser.parse` swallows errors to `nil` by design, so to see *why* a
    parse returned `nil`, set a breakpoint in its `catch` (temporary only).

## What's under test (current code paths)

| Path | Code |
| --- | --- |
| Availability gate | `BrainDumpParser.isAvailable` → `SystemLanguageModel.default.availability` |
| Parse → nil fallback | `BrainDumpParser.parse` returns `nil` on empty / unavailable / any generation error |
| Guided schema → wire | `Extraction` (`todos`/`commitments`/`shopping`/`blockers`/`ifThenPlans`/`observations`) → `toParsedBrainDump()` |
| Time-window cue guard | `normalizedTimeWindow` (mirror of server `parse_window`) |
| Path selection + escalation | `BrainDumpView.capture()` (on-device when available, else server) + the "server pass" button |
| Wire contract | `APIClient.braindump(parse:)` sends `actions` + `observations` |
| Server re-validation | `assistant.plan_preparsed` + `sensor.validate_observations` (no model call; `provider = on_device`) |

## Test matrix — inputs → expected

Type (or dictate) each ramble into the brain-dump sheet on-device. "Preview"
means what the review surface shows *before* you apply — nothing is written until
Apply / Accept.

| # | Ramble | Expected on-device parse | Expected preview |
| --- | --- | --- | --- |
| 1 | "call the dentist, book the flights, we're out of milk" | `add_todo` ×2, `add_shopping` ×1 | 3 actions, provider **on_device**, 0 proposals |
| 2 | "dentist thursday at 2pm" | `add_commitment` (start_at `YYYY-MM-DD 14:00`) | 1 action; the date resolves to the correct local Thursday |
| 3 | "when I get home I'll take my meds" | `add_if_then` (`event: arrive_home`) | 1 action; applying creates an if-then plan |
| 4 | "between 9 and 5 I'll keep my phone on do-not-disturb" | `add_if_then` (`time_window: 09:00-17:00`) | 1 action (window parsed) |
| 5 | "after dinner I'll tidy the kitchen" | **if-then dropped** — "after dinner" isn't an `HH:MM-HH:MM` cue | 0 actions from this line (dropped locally, **no** server error) |
| 6 | "I blew off admin again today" | `observations`: one `episode` (`episode_type: task`, `outcome: miss`) | 0 actions, **1 pending proposal** in the review queue |
| 7 | "stop nudging me after 9pm" | **not captured on-device** (settings `state` change is deliberately server-only) | empty/quiet on-device; a **server pass** should surface it as a proposal |
| 8 | "" / a shrug of filler with nothing actionable | empty parse | quiet — no misleading "I didn't find anything", no crash |

## Edge / branch checklist

- [ ] **Model available** → capture uses on-device; provider reads `on_device`;
      `capture_funnel.on_device` increments; body carries `parse`, not `text`.
- [ ] **Apple Intelligence OFF** (toggle it off in Settings) → `isAvailable` is
      `false`, capture falls back to the **server text parse**; provider is
      `anthropic`/`ollama`, funnel increments the cloud bucket, body carries `text`.
- [ ] **Model still downloading** → same graceful fallback as "off" (not a hang or
      crash).
- [ ] **Guardrail refusal / generation error** (an input the model refuses) →
      `parse` returns `nil` → falls back to server rather than losing the capture.
- [ ] **"Server pass" button** (shown after an on-device result) → re-runs the
      same ramble through the cloud agent; catches the behavioral asides (#6) and
      settings changes (#7) the on-device pass leaves alone; funnel moves to cloud.
- [ ] **Empty on-device result** stays silent and still offers the server pass
      (that's when the raw text first leaves the device).
- [ ] **Privacy:** across every on-device capture, confirm via the proxy that the
      raw ramble text is **never** in the request body — only the structured `parse`.
- [ ] **Apply / Accept still gated:** applying actions writes via
      `/assistant/apply`; accepting a proposal via `/proposals/{id}/accept`. Nothing
      is written on capture.
- [ ] **Quality spot-check:** across ~10 varied real rambles, extraction is
      "good enough" — items are terse, in the user's words, nothing invented; note
      systematic misses (e.g. times off by a zone) for prompt tuning.

## Recording results

Copy this block into the PR / issue when you run it:

```
Device:            iPhone <model>
iOS:               <version>
Apple Intelligence: on, model downloaded
App build:         <branch @ short-sha>
Server:            <host>
Date:              <YYYY-MM-DD>

Matrix  1..8:      [ ] pass  (notes: …)
Edge checklist:    [ ] all pass  (notes: …)
capture_funnel before/after: on_device <n>→<n>, cloud <n>→<n>
Extraction quality (10 rambles): <good / issues: …>
```

## Exit criteria

Consider the on-device path **verified** when the matrix and edge checklist pass,
the funnel confirms `on_device` moved on on-device captures (and cloud moved on
the server pass), the proxy confirms no raw text leaves the device on the
on-device path, and extraction quality on real rambles is acceptable. Until then,
treat on-device extraction as **unverified at runtime** — the automated tests
only prove the wire/validation layer, never the model.
