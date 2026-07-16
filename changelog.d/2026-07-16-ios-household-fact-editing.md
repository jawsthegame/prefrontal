- **iOS: edit household facts from the phone** ✅ (follows the iOS Household UI) —
  the roster's reference facts were read-only; now each member (and the
  household-wide "Everyone" block) carries a ＋ to **add** a fact, and a long-press
  on any fact **edits** its value or **removes** it. The editor picks a category
  from the server vocab (`vocab.fact_categories`, labelled to match the backend's
  `FACT_CATEGORY_LABELS`), takes a field + value, and writes `POST
  /household/facts` (upsert on child/category/item) or `/household/facts/clear`;
  because that triple is the key, an edit keeps the category + field fixed and
  changes the value. The "Everyone" block is always shown so there's a home for
  household-wide facts (trash day, address, Wi-Fi). New `FactEditorSheet` +
  `setFact`/`clearFact` in `Networking/Endpoints.swift`; `vocab` now decoded off
  the sheet. Client-only (build on a Mac); endpoints are the existing household
  router.
