- **People queue: far fewer false-positive names** ✅ — the deterministic name
  extractor (`prefrontal.people.extract_names`) treated *any* Title-Case run of
  2–3 words, and any cued lone Title-Case word, as a candidate person. Real mail
  and calendar text is dominated by capitalized noun-phrases that aren't people
  ("Weekly Report", "Order Confirmation", "Field Trip", "United Airlines"), so the
  identify queue filled with almost-all false positives. The extractor now drops a
  candidate whose every token is a generic non-name word (`_COMMON_WORDS`) or that
  contains an organization marker (`_ORG_MARKERS`, e.g. "Bank", "Airlines",
  "University"), while a real name survives because at least one of its tokens is
  neither. Both lexicons deliberately exclude any word that is also a plausible
  name (Bill, Mark, Grace, May, …), so a real person is never dropped to spare a
  generic phrase. Purely heuristic — no model call — so the ingest hot path stays
  snappy. Covered by new cases in `tests/test_people.py`.
