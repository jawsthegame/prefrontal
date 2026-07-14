- **iOS: Context Pack situation tools** ✅ — the enabled packs' on-demand
  situation tools (the Parent pack's **School run**, **Pack the bag**, and
  **Sick-day replan**) are now reachable natively, mirroring the web dashboard's
  Situations card. A self-loading **Situations** card on the **Today** tab lists
  the tools from `GET /packs/situations` and **stays hidden** when no pack
  contributes any (so it's invisible unless a pack like `parent` is enabled);
  tapping **Check** runs one (`POST /packs/situations/{tool}`) and renders its
  tool-specific result inline — the school-run departures, the pack-the-bag
  get-ready checklists, or the sick-day pressing items + first step. New
  `SituationsCard.swift`, `SituationTool`/`SituationResult` models, and
  `APIClient.situations()` / `.runSituation(tool:)`.
