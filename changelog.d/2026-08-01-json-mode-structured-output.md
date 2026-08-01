- **JSON call sites request the model's native JSON mode** ✅ — the local path is a
  summarizer / JSON-extractor / sensor, and until now every structured call leaned
  entirely on the tolerant `llm_json.extract_json` layer to rescue an object from a
  model that ignored "reply with only JSON". `Generator.generate` now takes an
  optional `format` argument; the local `OllamaClient` forwards `format="json"` to
  Ollama's structured-output constraint (the cloud `AnthropicClient` accepts and
  ignores it), so a capable model emits a single valid JSON value the extractor
  parses on its first candidate. A new signature-safe `llm_json.generate_text` shim
  funnels the requests and passes `format` (and `num_ctx`/`timeout`) only to a
  backend that declares it — so a minimal injected client, in prod or a test double,
  is simply called without it and the tolerant extractor still covers the rest. The
  `generate_json` facade and the direct JSON call sites (triage, mail triage, todo
  augment/decompose, delegation match/prep/question, autorun, sensor, projects,
  clarify, people, reschedule, communication-translation) all route through it;
  extraction stays as the safety net. Covered by new cases in `tests/test_ollama.py`
  (the `format` wire shape) and `tests/test_llm_json.py` (JSON-mode requested when
  supported, silently dropped when not).
