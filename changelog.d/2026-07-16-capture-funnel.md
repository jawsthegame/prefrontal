- **Brain-dump capture funnel: measure the on-device vs. escalated split** ✅ —
  the on-device Foundation-Model parse (roadmap M1) is the cheap/private path and
  raw text escalates to a server model, but `provider` was only ever *reported* in
  the `POST /braindump` response and never *recorded* — so there was no way to tell
  whether the private path was actually carrying the load. Each capture now stamps
  one best-effort `feature_events` row (`feature="braindump"`, `event="invoked"`,
  `source=` the handling provider — `on_device` on the parse path, else the
  escalated `anthropic`/`ollama`), and `build_stats` rolls the window's events into
  a new **`capture_funnel`** view on `GET /stats/data`: total captured, on-device
  vs. escalated counts, the on-device share (`None` until the first capture, not a
  misleading 0%), and the raw per-provider breakdown. Telemetry is wrapped so a
  logging failure never blocks a capture. New `capture_provider_counts()` repo
  query and `CAPTURE_FEATURE`/`ON_DEVICE_SOURCE` constants in `braindump.py`;
  covered by `tests/test_stats.py` and `tests/test_braindump.py`. (Surfacing the
  funnel as an Insights-page card is a follow-up; the data ships now.)
