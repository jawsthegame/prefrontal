# Project memory

## Meta (Ray-Ban) glasses integration
- The glasses are a **closed platform** — no third-party SDK, can't run code on them
  or trigger/read "Hey Meta". Any integration routes **through the phone**.
- Chosen path: hands-free **voice capture** → the LLM-as-sensor. A dictated message
  ("Hey Meta, send a message to Prefrontal: …") is bridged to `POST /observe`, where
  it lands as a *pending* proposal to review later (propose-then-confirm safety model
  is unchanged).
- Implementation lives in:
  - `prefrontal/sensor.py` → `normalize_voice_note()` strips a leading spoken address
    ("Prefrontal, …" / "Hey Prefrontal …" / "send a message to Prefrontal that …")
    before the note reaches the model. Conservative: only strips a prefix that *names*
    the assistant and is set off as an address. Wired into `extract_candidates`.
  - `deploy/n8n/observe-ingest.workflow.json` — template bridge (inbound message →
    `/observe` → speakable confirmation). Needs a real messaging trigger wired in
    (WhatsApp Business Cloud is the most reliable route the glasses can reach).
  - `deploy/meta-glasses.md` — the full recipe.
- Bonus, near-free: route the existing TTS delivery channel to the glasses' speakers
  so nudges/briefing/panic-first-step are spoken hands-free.
- Not possible: triggering Meta AI, pushing to a glasses display, reading glasses
  camera/GPS from a third party.
- Work was done on branch `claude/meta-glasses-integrations-8hne7c` (no PR opened).
