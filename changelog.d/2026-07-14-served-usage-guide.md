- **Usage guide, served on the box** ✅ — the full usage guide (`docs/guide.md`)
  is now reachable as a web page at **`GET /manual`** (e.g.
  `http://<mini>.tailnet.ts.net:8000/manual` over Tailscale), rendered live from
  that single Markdown source so the page never drifts from the doc. Unauthenticated
  and data-free like the other web surfaces; styled, theme-aware, and self-contained
  (no CDN). It sits alongside `/guide` (the per-module new-user walkthrough) and
  `/docs` (FastAPI's API explorer). Also refreshed the guide itself for the newest
  capabilities — **if-then plans** (implementation intentions) and **emotion
  regulation** (with its crisis-safety boundary). Rendering uses the pure-Python
  `markdown` dependency, degrading to raw text if it's ever absent. Covered by
  `tests/test_usage_guide.py`.
