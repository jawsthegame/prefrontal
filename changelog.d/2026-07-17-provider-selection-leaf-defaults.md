- **Selectable agents now reach Claude on their default paths** ✅ — the sensor,
  profile-summarizer, and mail-triage helpers each defaulted their inference client
  to a hard-wired `OllamaClient.from_settings()` when none was injected, so those
  agents — all listed in `KNOWN_AGENTS` and selectable via `ANTHROPIC_AGENTS` —
  could never reach Claude on any non-router path (a CLI coaching tick, a background
  mail ingest, an un-injected caller), regardless of configuration. They now build
  the client through `ProviderResolver.from_settings().client(<agent>)`
  (`sensor` → `SENSOR`, `summarize_profile` → `SUMMARIZER`, mail ingest/triage →
  `TRIAGE`), so opting an agent into `ANTHROPIC_AGENTS` routes its default path to
  Claude and still falls back to local Ollama when it isn't opted in or Anthropic is
  unavailable. (The generic `llm_json.generate_json` default is unchanged — it has no
  single owning agent; its callers inject the client.) Covered by a new default-path
  routing case in `tests/test_provider.py`.
