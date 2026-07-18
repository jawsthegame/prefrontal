- **Vacation mode: native iOS UI** ✅ — vacation mode is now usable from the
  phone, not just the CLI/API. A **Settings → Vacation mode** toggle
  (`GET`/`POST /vacation`) is the manual escape hatch for a staycation; a **Today
  banner** ("🏝️ Vacation mode — nudges eased off") with a one-tap **Resume** keeps
  the quiet visible (never a mystery); and the `vacation_suggest` push category is
  registered so the server's **`🏝️ Ease off`** suggestion button actually renders
  natively (it was a buttonless banner before, like `away_proposal`). Adds a
  `Vacation` model + `vacation()`/`setVacation()` client methods, covered by
  `PrefrontalTests/APIClientTests.swift`. iOS-only (builds on a Mac; gated by the
  `ios.yml` SwiftLint + typecheck + `xcodebuild test` CI).
