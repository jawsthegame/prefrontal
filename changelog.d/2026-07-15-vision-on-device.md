- **On-device vision capture** ✅ — the photo → structured-items flow (`POST
  /vision`, `prefrontal vision`) is now **local-first**. `OllamaClient` gained a
  `describe_image` that reads a photo with a local multimodal model (e.g. `llava`,
  `llama3.2-vision`) via Ollama's `/api/generate` `images` array, and a
  `can_describe_images` gate (a vision model configured *and* installed). A new
  `vision` agent in `ProviderResolver.select_vision` prefers the on-device model
  when it can see and falls back to the cloud Anthropic model otherwise — so a
  photo never leaves the host once a local vision model is configured. Listing
  `vision` in `ANTHROPIC_AGENTS` inverts the preference to cloud-first (still
  falling back to local without a key). The endpoint/CLI 503 only when *neither*
  backend can see, and the response reports which read the image
  (`provider.vision`). Config: `OLLAMA_VISION_MODEL` (empty = cloud fallback).
  Covered by a new `tests/test_ollama.py` (vision wire shape + routing gate) plus
  `select_vision` cases in `tests/test_provider.py` and local/cloud routing in
  `tests/test_vision.py`. Also documents the (previously undocumented) `/vision`
  endpoint in the guide.
  *Still ahead: native camera/Photos capture in the iOS app feeding this endpoint.*
