- **iOS: emotion-regulation support** ✅ — the native app now surfaces the
  `emotion_regulation` module (the *feeling* side of a hard moment, the sibling to
  Panic and the day-shaped encouragement layer), previously reachable only over
  HTTP. A calm **"Having a hard moment?"** row on the **Today** tab opens an
  in-the-moment support sheet: reach in with one tap or a few words and the server
  returns **one** brief, evidence-matched micro-skill (ACT / DBT distress-tolerance
  / self-compassion), rendered **verbatim** with its tradition named and the
  inferred feeling shown when specific; a **Try another** rotates skills. The
  client mirrors the server's safety boundary — it keys its framing off `kind`, so
  a response the server screened as a crisis (`kind == "crisis"`) renders as
  resources with one-tap **Call 988 / Text 988**, never a coping skill and never a
  "try another". New `EmotionSupportView.swift`, an `EmotionSupport` model, and
  `APIClient.emotionSupport(text:)` (`POST /emotion/support`), covered by
  `APIClientTests`.
