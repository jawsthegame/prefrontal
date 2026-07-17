- **Webhook routers reach the local model through one place** ✅ — `RouterServices`
  carried a `ollama` field that was the *same* short-timeout inference client the
  `ProviderResolver` already holds, and eight routers used `services.ollama`
  directly. That duplicated the local client alongside the per-agent selector and
  blurred which paths are deliberately local. The redundant field is removed:
  selectable agents keep using `provider.client("<agent>")`, and the
  deliberately-local paths (todo/mail decompose, clarify, calendar classify, outing
  anchor, impulse capture, coaching tick) now take the local client via
  `provider.ollama` — so the local inference client is reached through the provider,
  not a parallel bundle field. Behavior is unchanged (same client object); the
  distinct longer-timeout `summarizer` client stays, as the heavy-generation client
  and the `summarizer` agent's explicit local fallback.
