- **Blockers: dashboard card + iOS "Waiting on you"** ✅ (builds on the blockers
  tracker) — the visual surfaces for who's blocked on you. The web dashboard gains
  a **Waiting on you** card right under Todos: open blockers longest-wait first,
  a priority chip, a one-line add row (person · what · priority), and a one-tap
  **Delivered** to resolve. The native iOS app gains a matching self-loading card
  on the **Today** tab (`ios/Prefrontal/Views/BlockersCard.swift`) with a ＋
  capture sheet and inline resolve, plus `Blocker` model + `/blockers` endpoints
  on `APIClient`. Both read/write the same `/blockers` API. Web card verified
  end-to-end in a headless browser (add + resolve); iOS covered by an `APIClient`
  decode test (`ios/PrefrontalTests/APIClientTests.swift`).
