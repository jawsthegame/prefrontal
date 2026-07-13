- **Conflict-free changelog via fragments** ✅ — the changelog was a rebase
  hotspot: every change prepended a bullet to the same spot (top of
  `## Recently shipped`), so any two in-flight branches collided there and every
  rebase re-conflicted. Changes now drop a standalone fragment under
  `changelog.d/<date>-slug.md` instead of editing `CHANGELOG.md` — two different
  filenames never conflict, and a branch only ever *adds* a file.
  `scripts/collate_changelog.py` folds the accumulated fragments into
  `CHANGELOG.md` newest-first and deletes them (`--check` for CI, `--keep` to
  retain) — the one place the shared file is touched, run periodically rather than
  per PR. See `changelog.d/README.md`; covered by
  `tests/test_changelog_fragments.py`.
