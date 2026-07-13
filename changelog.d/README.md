# `changelog.d/` — changelog fragments

Add a **fragment file here instead of editing [`CHANGELOG.md`](../CHANGELOG.md)
directly.** Every change used to prepend a bullet to the same spot — the top of
`## Recently shipped` — so any two in-flight branches collided on those exact
lines and every rebase re-conflicted. Two branches adding two *different* files
never conflict, and a branch only ever *adds* a file, so the changelog stops
being a rebase hotspot.

## Adding an entry

Create one Markdown file per change, named `YYYY-MM-DD-slug.md` (the date prefix
makes fragments sort newest-first when they're folded in):

```
changelog.d/2026-07-13-situation-tools.md
```

Its body is the changelog bullet(s) exactly as they'd read under
`## Recently shipped` — verbatim, so it can be one bullet or a short group:

```markdown
- **Context Packs: pack situation tools** ✅ — the registry backbone for a
  pack's *situation tools*: read-only, on-demand questions a pack answers from
  live data. … Covered by `tests/test_situations.py`.
```

That's the whole workflow for a PR. Don't touch `CHANGELOG.md`.

## Folding fragments in (maintenance, not per-PR)

Periodically — not on every PR — one person collates the accumulated fragments
into `CHANGELOG.md` and deletes them:

```
python scripts/collate_changelog.py          # fold in (newest first) + delete
python scripts/collate_changelog.py --check   # list pending; non-zero if any (CI)
python scripts/collate_changelog.py --keep     # fold in but keep the fragments
```

This is the *only* step that edits the shared `CHANGELOG.md`, and it runs rarely
and by a single person — so the per-PR conflict is gone.
