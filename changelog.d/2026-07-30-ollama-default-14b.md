- **Local summarizer defaults to `qwen2.5:14b`** ✅ — the default `OLLAMA_MODEL`
  bumps from `llama3.1:8b` to `qwen2.5:14b` (Apache-2.0, 128K context, first-class
  Ollama support). The whole local path is a summarizer / JSON-extractor / sensor,
  and the biggest win is instruction-following: a 14B model honors "reply with only
  JSON" far more reliably than the 8B did, so the tolerant `llm_json.extract_json`
  layer has less to paper over, and the big-pasted-context delegation digest gets
  real headroom. Operators on a 16GB box can still set `OLLAMA_MODEL=llama3.1:8b`;
  the new default assumes 24GB+ (see `.env.example` and `docs/deployment.md`).
  Touches `config.py`, `integrations/ollama.py`, `.env.example`, `docs/guide.md`,
  and `docs/deployment.md`.
