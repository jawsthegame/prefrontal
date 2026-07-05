# Meta (Ray-Ban) glasses → Prefrontal

Hands-free capture: get a thought out of your head the moment you have it —
without a phone, a tap, or opening anything — and let it land as a *pending*
proposal you review later.

> **The honest constraint first.** Ray-Ban Meta glasses are a closed platform:
> there's no third-party SDK, no way to run code on them, and no way to
> programmatically trigger "Hey Meta" or read its replies. So there is no
> *direct* integration. Everything here routes **through your phone**, using a
> capability the glasses already have — dictating a message by voice — and a
> small bridge into an endpoint Prefrontal already exposes. Within that limit it
> works well, and it's squarely the problem Prefrontal exists to solve: capture
> at the moment of thought, zero friction.

---

## What this does

You say, hands-free:

> "Hey Meta, send a message to Prefrontal — dentist wants me back in six months."

The glasses dictate that into a message thread. A bridge picks it up and POSTs
the text to **`/observe`** — the [LLM-as-sensor](../docs/guide.md). The sensor
reads it and files a **pending candidate** (a todo, a preference, a behavioral
observation) that you accept or reject later at `GET /proposals` or in the
dashboard. Nothing authoritative is written until you confirm it, so a garbled
transcription can never corrupt your memory layer — the whole
propose-then-confirm safety model is unchanged.

The bridge can reply with a one-line confirmation, and the glasses **read
incoming messages aloud** — so you hear *"Captured: buy milk. Review it when
you're back."* in your ear. A hands-free capture is never a silent drop.

Why `/observe` and not `/todos`? Because you don't know, mid-walk, whether the
thought is a task, a preference ("I keep skipping lunch on busy days"), or an
outcome to log. The sensor classifies it for you into the allowlisted shapes and
holds it for review — exactly right for a low-signal, hands-free stream.

---

## The address prefix is handled for you

Dictation-to-message flows prepend a vocative: *"Prefrontal, …"*, *"Hey
Prefrontal …"*, *"send a message to Prefrontal that …"*. The server strips that
leading address itself (`normalize_voice_note`) before the sensor reads the
note, so **forward the dictated line verbatim** — don't try to trim it in the
bridge. It's conservative: it only removes a prefix that *names* the assistant
and is set off as an address, so a note that merely starts with the word
("Prefrontal is running slow today") or a bare verb ("remember to call the
dentist") is left intact.

---

## Wiring it up

The glasses can dictate into WhatsApp, Messenger, or Instagram by voice. The job
is to get that one thread's messages to `POST /observe`. That bridge belongs in
**n8n** (already core to Prefrontal), not in iOS Shortcuts — iOS has no reliable
"message received from a third-party app" trigger, and n8n has first-class
inbound triggers for exactly these channels.

### Recommended: n8n message bridge

1. Import [`n8n/observe-ingest.workflow.json`](n8n/observe-ingest.workflow.json)
   (or `prefrontal n8n push` — see [`../docs/n8n-sync.md`](../docs/n8n-sync.md)).
2. Replace the **Inbound message** webhook node with your channel's real trigger:
   - **WhatsApp Business Cloud** (n8n's *WhatsApp Trigger*) — the most reliable.
     Register a WhatsApp Business number, save it as a contact named
     **Prefrontal**, and "Hey Meta, send a message to Prefrontal" routes to it.
   - **Telegram** — the glasses don't send Telegram directly, but if you relay
     WhatsApp→Telegram (or just prefer Telegram), the *Telegram Trigger* works.
   - **Email** — "Hey Meta, send an email to …" is also hands-free; point an
     inbox/IMAP trigger at a dedicated address.
3. Edit the **Extract dictated text** node's field pick to match your trigger's
   payload (WhatsApp puts the body at `messages[0].text.body`, Telegram at
   `message.text`, etc.).
4. Set the workflow's `PREFRONTAL_BASE_URL` env + the **Prefrontal Token**
   credential (a per-user token, so proposals land in *your* queue).
5. **Wire the reply back to your channel** so the confirmation is spoken in your
   ear: after **Speakable confirmation**, add your channel's *send message* node
   (WhatsApp/Telegram) replying to the same chat with `{{ $json.reply }}`.
6. Toggle the workflow **Active**.

Quick test the bridge target from the Mac, exactly as the workflow will:

```sh
curl -s -X POST http://localhost:8000/observe \
  -H "X-Prefrontal-Token: $PREFRONTAL_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"Prefrontal, I keep skipping lunch on busy days"}' | python3 -m json.tool
```

You'll get back `{"count": …, "proposals": [{"summary": …}]}`. Review and accept
with `GET /proposals` then `POST /proposals/<id>/accept` (or the dashboard). If
`count` is `0`, the sensor found nothing to file *or* the local model was
unreachable — an honest no-guess result, and the spoken confirmation says so.

---

## Bonus, nearly free: nudges in your ear

The glasses are open-ear Bluetooth speakers. Prefrontal's delivery layer already
has a **TTS channel** (`prefrontal coach --deliver`, `docs/coaching-agent.md`).
If your phone routes that audio to the glasses, the morning briefing, escalating
nudges, and — the one that matters most — **panic mode's single first step** get
*spoken* hands-free. No new code; it's a delivery-routing choice on the phone.
This is a natural pair with the capture bridge above: talk to it, hear it back.

---

## What's genuinely not possible

- Triggering "Hey Meta" or reading Meta AI's replies programmatically.
- Pushing a notification to the glasses' (absent) display.
- Reading the glasses' camera/GPS from a third party — location-aware modules
  still ride the phone's location, not the glasses'.
- Anything the glasses do *automatically*. Every path here is you-initiated and
  phone-relayed.

Photo capture ("Hey Meta, take a photo" of a flyer/whiteboard → syncs to phone →
an iOS Photos automation OCRs it → `POST /observe` or `/todos`) is a viable
second bridge on the same pattern; it just lives on the iOS side. See the
capture shortcuts in [`ios-shortcut.md`](ios-shortcut.md).
