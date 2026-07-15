- **Photo → structured items** ✅ — the vision sibling of the voice brain-dump.
  Snap a photo of anything already written down (a whiteboard after a meeting, a
  school newsletter, a scribbled shopping list, a receipt) and it's read to text
  by the multimodal model, then fanned out through the *same* capture paths: the
  NL editing assistant turns actionable bits into a **previewable** action list,
  and the LLM sensor turns behavioral asides into **pending** candidates. It owns
  no new capability and no new safety model — the image is just a transcript
  feeding `braindump.plan_braindump`, so actions apply via `POST /assistant/apply`
  and candidates via `POST /proposals/{id}/accept`; a blurry, misread photo can
  never silently mutate the store. New `AnthropicClient.describe_image` (the cloud
  multimodal read), `prefrontal/vision.py` (`plan_vision` = `describe_image` +
  brain-dump fan-out), a `POST /vision` endpoint, and a `prefrontal vision PATH`
  CLI (`--apply` to execute edits immediately). Vision is Anthropic-only today, so
  there's no local fallback: an unavailable backend is a 503, not a guess. Covered
  by `tests/test_vision.py` and a new SDK-boundary `tests/test_anthropic.py`.
  *Still ahead: routing the read through an on-device multimodal model, and native
  camera/Photos capture in the iOS app feeding this endpoint.*
