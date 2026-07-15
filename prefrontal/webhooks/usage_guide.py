"""Serve the human usage guide (`docs/guide.md`) as a styled web page.

The guide is a single Markdown source of truth in the repo; rather than maintain a
second HTML copy, this renders that file on request into a self-contained,
Tailscale-reachable page (``GET /manual`` — *not* ``/docs``, which is FastAPI's
Swagger UI). Local-first and offline: rendering is a pure-Python pass with no
network and no CDN assets (styles are inlined).

Rendering degrades gracefully so the route can never 500:

- if the ``markdown`` package isn't importable (e.g. a deploy that pulled new code
  but hasn't reinstalled deps yet) **or the render itself raises**, the raw Markdown
  is shown escaped in a ``<pre>`` with a one-line note — readable, just unstyled;
- if the guide file can't be found (e.g. run from a packaged wheel that ships only
  ``prefrontal/``, not ``docs/``), a short explanatory page is served instead.

The guide is trusted first-party content, but since ``/manual`` shares an origin
with authenticated surfaces, the rendered HTML is scrubbed of active content
(``<script>``/``<style>``/inline handlers/``javascript:`` URLs) as defense in
depth — so a stray raw-HTML snippet in the doc can never execute.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path

from prefrontal.log import get_logger

logger = get_logger(__name__)

#: Active-content patterns stripped from the rendered HTML as defense in depth
#: (see :func:`_scrub_active_html`). Not a general sanitizer — the guide is
#: first-party — just the realistic XSS vectors, since ``/manual`` shares an origin
#: with authenticated pages.
_ACTIVE_HTML_RE = re.compile(
    r"""(?isx)
    <\s*(script|style|iframe|object|embed)\b[^>]*>.*?</\s*\1\s*>  # element + contents
    | <\s*(?:script|style|iframe|object|embed|link|meta|base)\b[^>]*>  # lone/void openers
    | \son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)                  # inline event handlers
    | javascript:                                                # javascript: URIs
    """
)

#: Candidate locations for docs/guide.md — the deployment runs from a source
#: checkout (launchd over the repo), where the first resolves; the others are
#: defensive fallbacks. Resolved lazily and not cached to disk so editing the
#: guide and reloading shows the change.
_GUIDE_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "docs" / "guide.md",  # repo_root/docs
    Path.cwd() / "docs" / "guide.md",
)

_PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1rem 5rem;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif;
  color: #1c1c1e; background: #fbfbfd;
}
main { max-width: 46rem; margin: 0 auto; }
h1, h2, h3, h4 { line-height: 1.25; margin: 2.2rem 0 0.8rem; font-weight: 650; }
h1 { font-size: 2rem; margin-top: 0; }
h2 { font-size: 1.45rem; padding-top: 1rem; border-top: 1px solid #e6e6ea; }
h3 { font-size: 1.15rem; }
p, li { color: #2c2c2e; }
a { color: #0a6cff; text-decoration: none; }
a:hover { text-decoration: underline; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.88em;
  background: #eef0f4; padding: 0.1em 0.35em; border-radius: 4px; }
pre { background: #f4f5f7; border: 1px solid #e6e6ea; border-radius: 8px; padding: 0.9rem 1rem;
  overflow-x: auto; }
pre code { background: none; padding: 0; font-size: 0.85em; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; display: block; overflow-x: auto; }
th, td { border: 1px solid #e0e0e6; padding: 0.45rem 0.7rem; text-align: left;
  vertical-align: top; }
th { background: #f0f1f5; }
blockquote { margin: 1rem 0; padding: 0.2rem 1rem; border-left: 3px solid #c7c7cc; color: #48484a; }
hr { border: none; border-top: 1px solid #e6e6ea; margin: 2rem 0; }
.masthead { max-width: 46rem; margin: 0 auto 1.5rem; font-size: 0.85rem; color: #8a8a8e; }
.note { max-width: 46rem; margin: 0 auto 1rem; padding: 0.6rem 0.9rem; border-radius: 8px;
  background: #fff6e5; border: 1px solid #f2d98a; color: #6b5300; font-size: 0.9rem; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e6ea; background: #121214; }
  h2 { border-top-color: #2a2a2e; }
  p, li { color: #d0d0d4; }
  a { color: #4c9dff; }
  code { background: #26262b; }
  pre { background: #1b1b1f; border-color: #2a2a2e; }
  th, td { border-color: #2a2a2e; }
  th { background: #1f1f24; }
  blockquote { border-left-color: #3a3a3e; color: #a0a0a6; }
  hr { border-top-color: #2a2a2e; }
  .note { background: #2b2410; border-color: #5a4a12; color: #e8cf7a; }
}
"""

_SHELL = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<title>Prefrontal — Usage Guide</title><style>{css}</style></head>"
    "<body><div class=\"masthead\">Prefrontal · usage guide · "
    "rendered from <code>docs/guide.md</code></div>{note}<main>{body}</main></body></html>"
)


def _read_guide() -> str | None:
    """The guide's Markdown source, or ``None`` if no candidate path exists."""
    for path in _GUIDE_CANDIDATES:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def _render_markdown(md_text: str) -> tuple[str, str]:
    """Render Markdown to an HTML fragment; return ``(body_html, note_html)``.

    Uses the ``markdown`` package (offline, pure-Python) with the table / fenced-code
    / heading-anchor extensions the guide relies on, then scrubs active content from
    the result (:func:`_scrub_active_html`). If the package isn't importable **or the
    render itself raises**, falls back to the escaped raw source in a ``<pre>`` plus a
    visible note — so the page always serves rather than 500-ing.
    """
    try:
        import markdown  # noqa: PLC0415 — optional at runtime; fallback below

        rendered = markdown.markdown(
            md_text,
            extensions=["extra", "tables", "fenced_code", "sane_lists", "toc"],
            output_format="html5",
        )
        return _scrub_active_html(rendered), ""
    except ImportError:
        return _raw_fallback(
            md_text,
            "Showing the raw guide — install the <code>markdown</code> package (it's "
            "in the project dependencies) and reload for the formatted version.",
        )
    except Exception:  # noqa: BLE001 — a render glitch must degrade, never 500 the route
        logger.warning("usage-guide markdown render failed; serving raw", exc_info=True)
        return _raw_fallback(
            md_text, "Showing the raw guide — the formatted render hit an error."
        )


def _raw_fallback(md_text: str, note_msg: str) -> tuple[str, str]:
    """The escaped-raw-source body + a note, used when rendering can't run."""
    return f"<pre>{html.escape(md_text)}</pre>", f'<div class="note">{note_msg}</div>'


def _scrub_active_html(rendered: str) -> str:
    """Strip active content from rendered HTML (defense in depth over trusted docs).

    Not a general-purpose sanitizer — ``docs/guide.md`` is first-party — but because
    ``/manual`` shares an origin with authenticated pages, this removes the realistic
    XSS vectors (``<script>``/``<style>``/``<iframe>``/``<object>``/``<embed>``
    elements, inline ``on*=`` event handlers, ``javascript:`` URLs) so a stray raw-HTML
    snippet in the guide can never execute. Benign markup (tables, code, headings,
    links) is untouched.
    """
    return _ACTIVE_HTML_RE.sub("", rendered)


@lru_cache(maxsize=1)
def _missing_page() -> str:
    return _SHELL.format(
        css=_PAGE_CSS,
        note="",
        body=(
            "<h1>Usage guide unavailable</h1><p>The guide source "
            "(<code>docs/guide.md</code>) wasn't found next to the running server. "
            "It ships in the source checkout; if you're running from a packaged "
            "build, read it in the repository instead.</p>"
        ),
    )


def render_usage_guide_page() -> str:
    """The full HTML page for ``GET /manual`` (guide rendered, or a graceful fallback)."""
    md_text = _read_guide()
    if md_text is None:
        return _missing_page()
    body, note = _render_markdown(md_text)
    return _SHELL.format(css=_PAGE_CSS, note=note, body=body)
