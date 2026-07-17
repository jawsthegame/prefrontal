- **LLM JSON extraction no longer drops actions from a prose-wrapped array** ✅ —
  `prefrontal.llm_json.extract_json` picks the first balanced JSON span in a model
  reply, but the matcher always tried the `{…}` span before the `[…]` span
  regardless of which opener came first. A reply that was a bare array wrapped in
  prose (`here are the actions: [{…}, {…}]`) matched the `{` of its *first element*
  and collapsed to that single object — so the editing assistant, which supports a
  bare-array reply, silently lost every requested edit after the first (both
  `reply` and `actions` came back `None`, and it returned `("", [])`). The two
  balanced spans are now ordered by opener position — matching the docstring's
  "whichever appears first" — so a leading `[` wins. Objects, fenced blocks, and
  objects that merely *contain* an array are unaffected. Covered by new cases in
  `tests/test_llm_json.py`.
