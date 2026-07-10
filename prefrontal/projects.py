"""Suggest a project for an item, matched against project name + description.

Mirrors the todo **category** inference in :mod:`prefrontal.todos` exactly: a
deterministic keyword-overlap heuristic and an optional one-shot LLM JSON call,
local-first with graceful degradation. Triage calls :func:`suggest_project` right
where it already infers a todo's category, so a triaged item lands in the right
project ("permits, contractor quotes, tile" → *Kitchen Remodel*) without the user
filing it by hand.

Projects are opt-in: with no projects, or nothing clearing the confidence
threshold, the item stays unassigned.
"""
from __future__ import annotations

import re
from typing import Any

from prefrontal.integrations import Generator
from prefrontal.integrations.ollama import OllamaError
from prefrontal.llm_json import extract_json_object

#: Below this confidence a suggestion is dropped (item left unassigned). The bar
#: is deliberately high — a wrong auto-assignment is more annoying than none, and
#: the user can always assign by hand.
MIN_SUGGEST_CONFIDENCE = 0.6

#: Words too common to carry any project signal in the token-overlap fallback.
_STOPWORDS = frozenset(
    "the a an and or of to for in on at by with from re fwd your you please "
    "this that it is are be need needs about into".split()
)


def normalize_project_name(name: str) -> str:
    """Trim and collapse internal whitespace in a project name."""
    return re.sub(r"\s+", " ", name).strip()


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens of length ≥ 3, minus stopwords."""
    return {
        w for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) >= 3 and w not in _STOPWORDS
    }


def heuristic_project(
    title: str, notes: str | None, projects: list[dict[str, Any]]
) -> tuple[int | None, float]:
    """Best project by token overlap of the item vs each project's name+description.

    The deterministic fallback for when the model is unavailable (mirrors
    :func:`prefrontal.todos.heuristic_category`). Confidence is the fraction of a
    project's name+description tokens that appear in the item text, so a tight,
    specific description scores higher than a vague one. Returns ``(None, 0.0)``
    when nothing overlaps.
    """
    item = _tokens(f"{title} {notes or ''}")
    if not item:
        return None, 0.0
    best_id: int | None = None
    best_score = 0.0
    for project in projects:
        blurb = _tokens(f"{project.get('name', '')} {project.get('description') or ''}")
        if not blurb:
            continue
        overlap = len(item & blurb) / len(blurb)
        if overlap > best_score:
            best_id, best_score = int(project["id"]), overlap
    return best_id, best_score


def _project_lines(projects: list[dict[str, Any]]) -> str:
    """Render the candidate projects for the model, one ``id: name — desc [domain]`` line."""
    lines = []
    for p in projects:
        desc = (p.get("description") or "").strip()
        tail = f" — {desc}" if desc else ""
        lines.append(f"{p['id']}: {p['name']}{tail} [{p.get('domain', '')}]")
    return "\n".join(lines)


def _llm_project(
    title: str, notes: str | None, projects: list[dict[str, Any]], client: Generator
) -> tuple[int | None, float] | None:
    """Ask the model which project fits; ``None`` on provider failure (caller falls back)."""
    system = (
        "You file an incoming to-do into one of the user's projects, matching it "
        "against each project's name and description. Reply with ONLY a JSON "
        'object, no prose: {"project_id": <the id of the best-fitting project, or '
        'null if none genuinely fit>, "confidence": <0.0-1.0>}. Prefer null over a '
        "weak guess.\n\nProjects:\n" + _project_lines(projects)
    )
    prompt = f"To-do: {title}"
    if notes:
        prompt += f"\nDetail: {notes}"
    try:
        reply = client.generate(prompt, system=system)
    except OllamaError:
        return None
    raw = extract_json_object(reply)
    valid_ids = {int(p["id"]) for p in projects}
    pid = raw.get("project_id")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid not in valid_ids:
        return None, 0.0
    conf = raw.get("confidence")
    ok = isinstance(conf, (int, float)) and not isinstance(conf, bool)
    confidence = float(conf) if ok else 0.0
    return pid, max(0.0, min(1.0, confidence))


def suggest_project(
    title: str,
    notes: str | None,
    projects: list[dict[str, Any]],
    *,
    client: Generator | None = None,
) -> int | None:
    """Suggest the best-matching active project's id for an item, or ``None``.

    Two layers, exactly like :func:`prefrontal.todos.augment_todo`'s category:
    the model first (one JSON call over the item text + the project list), the
    token-overlap heuristic when the model is unavailable. A suggestion is only
    returned when its confidence clears :data:`MIN_SUGGEST_CONFIDENCE`; otherwise
    ``None`` (the item stays unassigned — projects are opt-in).

    Args:
        title: The item's title.
        notes: Optional detail / body text.
        projects: The user's active projects as ``id``/``name``/``description``/
            ``domain`` dicts (from ``store.active_projects()``).
        client: An Ollama-/Anthropic-like client; ``None`` skips the model and
            uses the heuristic only.

    Returns:
        A project id, or ``None`` when nothing is a confident match.
    """
    if not projects:
        return None
    result = _llm_project(title, notes, projects, client) if client is not None else None
    if result is None:  # provider failure → deterministic fallback
        result = heuristic_project(title, notes, projects)
    project_id, confidence = result
    if project_id is None or confidence < MIN_SUGGEST_CONFIDENCE:
        return None
    return project_id
