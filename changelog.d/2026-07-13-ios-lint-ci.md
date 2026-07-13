- **iOS lint + compile CI** ✅ (first cut of #602) — the Swift app under `ios/`
  had no automated verification. Added a checked-in [`ios/.swiftlint.yml`](ios/.swiftlint.yml)
  and a macOS CI workflow (`.github/workflows/ios.yml`) that runs SwiftLint plus
  the `swiftc -typecheck` compile check from `ios/README.md`, gated to `ios/**`
  changes so Python-only PRs don't spend macOS minutes. Non-strict to start (only
  error-severity lint rules fail), so the never-linted codebase can land green and
  be tightened later. An XCTest unit-test target and a full `xcodebuild` build are
  the intended fast-follow (tracked in #602).
