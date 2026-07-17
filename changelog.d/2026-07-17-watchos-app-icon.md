- **watchOS: add the watch app icon** — the `PrefrontalWatch` target had no
  asset catalog, so it shipped with the blank placeholder icon. Adds
  `ios/PrefrontalWatch/Assets.xcassets` with a single-size (1024×1024) `AppIcon`
  reusing the phone's brand artwork — the green→teal "P" swirl on navy. The
  source is already alpha-free (watchOS forbids an alpha channel on app icons)
  and the glyph sits within the circular-mask safe area, so nothing important
  clips under the watch's round crop. Picked up automatically via the target's
  source path; the default `AppIcon` name needs no `project.yml` change.
  Client-only (build on a Mac).
