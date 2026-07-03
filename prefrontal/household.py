"""The shared household sheet — a single glanceable "everything about the kids".

Two co-parents share one household (:mod:`prefrontal.memory.repos.household`);
this module turns that household's data into one visible sheet, the way
:mod:`prefrontal.briefing` and :mod:`prefrontal.panic` turn a user's data into a
digest. Same two layers as those: a deterministic, model-free
:func:`build_sheet` / :func:`render_sheet` (fully testable), with no LLM pass —
assembly is cheap, so there is no cache (unlike ``profile_cache``); the sheet is
built on request.

Its reason to exist is **balancing invisible load**: every fact is stamped with
who set it and when, and the very first section is *what changed recently* —
the thing the oblivious parent sees first. See ``docs/household-sheet.md``.

Sections, in order (§6 of the spec):

1. **Recently changed** — the last few facts/agreements/star grants by their
   timestamp, each showing *what · who · when* ("Sam shoe size → 13 · Dana ·
   2d ago", "Sam · +2⭐ (Star chart) · Alex · 1h ago").
2. **Per-child facts** — grouped by category, one block per child (plus a
   household-wide block for ``child_id = 0`` facts).
3. **Standing agreements** — behaviour plans; star charts rendered from the
   optional ``structured`` JSON (thresholds → rewards) with live progress from
   the star ledger (total so far · how many to the next reward).
4. **Upcoming appointments** — the viewer's ``kind='child'`` commitments in the
   near window.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from prefrontal.commitments import KIND_CHILD
from prefrontal.impact import utcnow
from prefrontal.memory.repos.household import (
    FACT_CATEGORIES,
    FACT_CATEGORY_LABELS,
    HOUSEHOLD_WIDE,
)
from prefrontal.memory.store import MemoryStore

#: How many recent changes to surface — kept short so the load-balancing signal
#: (what the other parent hasn't seen) stays legible, not a full changelog.
MAX_RECENTLY_CHANGED = 6

#: How far ahead to surface child appointments.
UPCOMING_APPT_DAYS = 14


@dataclass(frozen=True)
class Change:
    """One recent write to the sheet, normalized across facts and agreements.

    Attributes:
        what: Human label ("Sam · shoe size → 13", "Star chart plan").
        who: Display name of whoever last set it (or ``None`` if unknown).
        when: Compact "how long ago" phrasing ("2d ago", "just now").
        at: The raw ``updated_at`` timestamp (for sorting/serialization).
    """

    what: str
    who: str | None
    when: str
    at: str


@dataclass(frozen=True)
class Appointment:
    """One upcoming child appointment (a ``kind='child'`` commitment)."""

    title: str
    when: str
    start_at: str
    location: str | None = None


@dataclass(frozen=True)
class HouseholdSheet:
    """The assembled shared sheet (deterministic; model-free).

    Attributes:
        household_name: The household's name, or ``None`` if unnamed.
        children: Roster dicts (id/name/birthday).
        recently_changed: Most-recent writes, newest first (the load surface).
        per_child: Ordered blocks ``{child_id, child_name, categories}`` where
            ``categories`` is ``[{category, label, items}]`` in
            :data:`FACT_CATEGORIES` order; the household-wide block (child_id 0)
            comes first when present.
        agreements: Standing-plan dicts (title/kind/body/child_name/structured),
            each also carrying ``star_total`` (the chart's running ledger total)
            and ``next_goal`` (the nearest unreached reward, or ``None``).
        upcoming: Upcoming child appointments, soonest first.
        counts: ``{children, facts, agreements, upcoming}`` for a summary line.
    """

    household_name: str | None
    children: list[dict[str, Any]] = field(default_factory=list)
    recently_changed: list[Change] = field(default_factory=list)
    per_child: list[dict[str, Any]] = field(default_factory=list)
    agreements: list[dict[str, Any]] = field(default_factory=list)
    upcoming: list[Appointment] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


# --- timestamp helpers (mirrors panic.py) ------------------------------------


def _parse_dt(ts: Any) -> datetime | None:
    """Parse a stored ``YYYY-MM-DD HH:MM:SS`` (naive UTC) timestamp, or ``None``."""
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _ago_phrase(ts: Any, now: datetime) -> str:
    """Compact "how long ago" phrasing for a past timestamp."""
    dt = _parse_dt(ts)
    if dt is None:
        return ""
    minutes = max(0.0, (now - dt).total_seconds() / 60.0)
    if minutes < 2:
        return "just now"
    if minutes < 90:
        return f"{round(minutes)}m ago"
    hours = minutes / 60.0
    if hours < 36:
        return f"{round(hours)}h ago"
    return f"{round(hours / 24.0)}d ago"


# --- build -------------------------------------------------------------------


def _fact_what(fact: dict[str, Any]) -> str:
    """Human label for a fact change: "Sam · shoe size → 13" (or household-wide)."""
    who_for = fact.get("child_name") or "Household"
    value = fact.get("value")
    tail = f" → {value}" if value else " cleared"
    return f"{who_for} · {fact['item']}{tail}"


def _star_grant_what(grant: dict[str, Any]) -> str:
    """Human label for a star grant: "Sam · +2⭐ (Star chart)"."""
    who_for = grant.get("child_name") or "Household"
    delta = int(grant.get("delta") or 0)
    sign = "+" if delta >= 0 else ""
    title = grant.get("agreement_title") or "chart"
    return f"{who_for} · {sign}{delta}⭐ ({title})"


def _appt_when(start: datetime | None, now: datetime) -> str:
    """Friendly "when" for an appointment ("today 3:00pm", "Thu 3:00pm", "in 5d")."""
    if start is None:
        return ""
    time_str = start.strftime("%-I:%M%p").lower()
    days = (start.date() - now.date()).days
    if days == 0:
        return f"today {time_str}"
    if days == 1:
        return f"tomorrow {time_str}"
    if 0 < days < 7:
        return f"{start.strftime('%a')} {time_str}"
    return f"{start.strftime('%b %-d')} {time_str}"


def build_sheet(store: MemoryStore, *, now: datetime | None = None) -> HouseholdSheet:
    """Assemble the structured household sheet from memory (deterministic).

    Args:
        store: A store **scoped to a user who is in a household** (its household
            methods resolve the shared scope). A user with no household will
            raise from the store's ``_household_id`` guard.
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).

    Returns:
        A :class:`HouseholdSheet`.
    """
    now = now or utcnow()

    household = store.household()
    children = store.children()
    facts = store.facts()
    agreements = store.agreements()

    star_totals = store.star_totals()

    # 1. Recently changed — facts + agreements + star grants, newest first.
    changes: list[Change] = []
    for f in facts:
        changes.append(
            Change(
                what=_fact_what(f),
                who=f.get("updated_by_name"),
                when=_ago_phrase(f.get("updated_at"), now),
                at=str(f.get("updated_at") or ""),
            )
        )
    for a in agreements:
        changes.append(
            Change(
                what=f"{a['title']} (plan)",
                who=a.get("updated_by_name"),
                when=_ago_phrase(a.get("updated_at"), now),
                at=str(a.get("updated_at") or ""),
            )
        )
    for grant in store.recent_star_awards(limit=MAX_RECENTLY_CHANGED):
        changes.append(
            Change(
                what=_star_grant_what(grant),
                who=grant.get("awarded_by_name"),
                when=_ago_phrase(grant.get("created_at"), now),
                at=str(grant.get("created_at") or ""),
            )
        )
    changes.sort(key=lambda c: c.at, reverse=True)
    recently_changed = changes[:MAX_RECENTLY_CHANGED]

    # 2. Per-child facts, grouped by category. Household-wide (child_id 0) first.
    per_child = _group_facts_by_child(facts, children)

    # 3. Agreements — parse structured JSON + attach the chart's star progress.
    agreements_out = [_prepare_agreement(a, star_totals.get(a["id"], 0)) for a in agreements]

    # 4. Upcoming child appointments — the viewer's kind='child' commitments.
    upcoming = _upcoming_child_appts(store, now)

    counts = {
        "children": len(children),
        "facts": len(facts),
        "agreements": len(agreements),
        "upcoming": len(upcoming),
    }
    return HouseholdSheet(
        household_name=(household or {}).get("name"),
        children=children,
        recently_changed=recently_changed,
        per_child=per_child,
        agreements=agreements_out,
        upcoming=upcoming,
        counts=counts,
    )


def _group_facts_by_child(
    facts: list[dict[str, Any]], children: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Group facts into per-child blocks, each block's items ordered by category."""
    names = {c["id"]: c["name"] for c in children}
    # Deterministic block order: household-wide (0) first, then children by name.
    order = [HOUSEHOLD_WIDE] + [c["id"] for c in children]
    blocks: list[dict[str, Any]] = []
    for child_id in order:
        owned = [f for f in facts if f["child_id"] == child_id]
        if not owned:
            continue
        categories = []
        for cat in FACT_CATEGORIES:
            items = [
                {
                    "item": f["item"],
                    "value": f.get("value"),
                    "updated_by_name": f.get("updated_by_name"),
                    "updated_at": f.get("updated_at"),
                }
                for f in owned
                if f["category"] == cat
            ]
            if items:
                categories.append(
                    {
                        "category": cat,
                        "label": FACT_CATEGORY_LABELS.get(cat, cat),
                        "items": items,
                    }
                )
        blocks.append(
            {
                "child_id": child_id,
                "child_name": names.get(child_id) or "Household",
                "categories": categories,
            }
        )
    return blocks


def _prepare_agreement(a: dict[str, Any], star_total: int = 0) -> dict[str, Any]:
    """Copy an agreement row with its ``structured`` JSON parsed and star progress.

    ``star_total`` is the chart's running ledger total; from it and the parsed
    goals we precompute the next unreached reward so the render (and any machine
    client) shows "7 stars · 3 to go → …" without re-deriving it.
    """
    parsed = parse_structured(a.get("structured"))
    ng = next_goal(parsed, star_total) if isinstance(parsed, dict) else None
    return {
        "id": a.get("id"),
        "title": a.get("title"),
        "kind": a.get("kind"),
        "body": a.get("body"),
        "child_name": a.get("child_name"),
        "structured": parsed,
        "star_total": star_total,
        "next_goal": ng,
        "updated_by_name": a.get("updated_by_name"),
    }


def _upcoming_child_appts(store: MemoryStore, now: datetime) -> list[Appointment]:
    """The viewer's near-window child appointments (``kind='child'`` commitments)."""
    horizon = now + timedelta(days=UPCOMING_APPT_DAYS)
    out: list[Appointment] = []
    for c in store.upcoming_commitments(limit=100):
        if c.get("kind") != KIND_CHILD:
            continue
        start = _parse_dt(c.get("start_at"))
        if start is None or start > horizon:
            continue
        out.append(
            Appointment(
                title=c.get("title") or "appointment",
                when=_appt_when(start, now),
                start_at=str(c.get("start_at")),
                location=c.get("location"),
            )
        )
    return out


# --- star chart goals (pure) -------------------------------------------------
#
# An agreement's `structured` JSON declares the reward goals; the running total
# lives in the household_stars ledger. These helpers turn (structured, totals)
# into the reached/next-goal facts the award endpoint and the render both need —
# kept pure so the "did this grant cross a reward line?" decision is unit-tested
# without a store, a transport, or a request.


def parse_structured(raw: Any) -> Any:
    """Parse an agreement's ``structured`` JSON string, or ``None`` if absent/bad."""
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _star_thresholds(structured: Any) -> list[tuple[int, str]]:
    """Sorted ``(count, reward)`` goal pairs from a chart's ``structured`` JSON.

    Tolerates the template's shapes: the count lives under the chart's ``unit``
    key, or ``stars``/``points``. Malformed entries are skipped, not raised on.
    """
    if not isinstance(structured, dict):
        return []
    raw = structured.get("thresholds")
    if not isinstance(raw, list):
        return []
    unit = structured.get("unit", "star")
    out: list[tuple[int, str]] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        if unit in t:
            n = t.get(unit)
        elif "stars" in t:
            n = t.get("stars")
        else:
            n = t.get("points")
        reward = t.get("reward")
        if isinstance(n, (int, float)) and reward:
            out.append((int(n), str(reward)))
    out.sort(key=lambda pair: pair[0])
    return out


def _unit_of(structured: Any) -> str:
    """The chart's counting unit (``"star"`` by default)."""
    return structured.get("unit", "star") if isinstance(structured, dict) else "star"


def newly_reached_goals(structured: Any, before: int, after: int) -> list[dict[str, Any]]:
    """Goals whose threshold the total crossed going ``before`` → ``after``.

    A goal ``n`` is newly reached when ``before < n <= after`` — so re-awarding
    at or above a threshold never re-fires it, and a single big grant that leaps
    past several thresholds reports each one (each is its own reward to hand out).
    """
    unit = _unit_of(structured)
    return [
        {"count": n, "unit": unit, "reward": reward}
        for n, reward in _star_thresholds(structured)
        if before < n <= after
    ]


def next_goal(structured: Any, total: int) -> dict[str, Any] | None:
    """The nearest not-yet-reached goal above ``total`` (with ``remaining``), or ``None``."""
    for n, reward in _star_thresholds(structured):
        if n > total:
            return {
                "count": n,
                "unit": _unit_of(structured),
                "reward": reward,
                "remaining": n - total,
            }
    return None


def star_congrats_text(child_name: str | None, goal: dict[str, Any]) -> str:
    """The celebration line sent to both parents when a reward goal is hit."""
    count = goal["count"]
    unit = goal["unit"]
    label = unit if count == 1 else f"{unit}s"
    who = child_name or "The kids"
    return f"🌟 {who} hit {count} {label} — reward unlocked: {goal['reward']}!"


def _resolve_child_name(store: MemoryStore, child_id: int) -> str | None:
    """The roster name for ``child_id`` (``None`` for household-wide id 0)."""
    if not child_id:
        return None
    return next((c["name"] for c in store.children() if c["id"] == child_id), None)


def award_stars_and_notify(
    store: MemoryStore,
    agreement_id: int,
    *,
    delta: int,
    awarded_by: int | None,
    note: str | None = None,
    settings: Any = None,
    client: Any = None,
) -> dict[str, Any] | None:
    """Award stars, detect crossed reward goals, and congratulate + notify both parents.

    The single code path behind every way a star can be given — the HTTP endpoint,
    the CLI, the one-tap notification button, and (via the caller) a scheduled
    prompt — so the "did this cross a goal? then tell both parents" rule lives in
    exactly one place.

    Returns ``None`` if the agreement isn't in the store's household (so a caller
    can 404). Raises :class:`ValueError` when a negative ``delta`` is applied to an
    earn-only chart. On success returns the new ``total``, the ``goals_reached``,
    the ``next_goal``, the resolved ``child_name``, and the per-parent ``notified``
    records (empty unless a goal was crossed).
    """
    agreement = store.agreement(agreement_id)
    if agreement is None:
        return None
    structured = parse_structured(agreement.get("structured"))
    if isinstance(structured, dict) and structured.get("earn_only") and delta < 0:
        raise ValueError("this chart is earn-only — stars can't be taken away")
    result = store.award_stars(
        agreement_id=agreement_id, delta=delta, awarded_by=awarded_by, note=note
    )
    assert result is not None  # agreement existence already confirmed above
    goals = newly_reached_goals(structured, result["before"], result["after"])
    child_name = _resolve_child_name(store, agreement.get("child_id") or 0)
    notified: list[dict[str, Any]] = []
    if goals:
        # Lazy import: delivery pulls in webhooks.notify, which would re-enter the
        # partially-initialized webhooks package if imported at module load.
        from prefrontal.integrations.delivery import (
            deliver_to_household,
            household_notice,
        )

        message = " ".join(star_congrats_text(child_name, g) for g in goals)
        notified = deliver_to_household(
            store,
            store.household_id_or_none(),
            household_notice(message, channel="sound"),
            settings=settings,
            client=client,
        )
    return {
        "agreement": agreement,
        "child_name": child_name,
        "total": result["after"],
        "delta": result["delta"],
        "goals_reached": goals,
        "next_goal": next_goal(structured, result["after"]),
        "notified": notified,
    }


# --- scheduled award prompts (pure) ------------------------------------------
#
# A chart can carry a `prompt` block in its `structured` JSON — a recurring
# "should <kid> get a star for <chart> today?" check-in on chosen weekdays at a
# time of day. The schedule lives on the chart (no new table); the sweep that
# fires it (a periodic check endpoint / CLI) uses `prompt_due`, and the chart's
# `last_prompted_at` column dedups to once per day. Weekdays are Python's
# `date.weekday()` ints: Mon=0 … Sun=6.


def _parse_hhmm(value: Any) -> time | None:
    """Parse a ``"HH:MM"`` 24-hour string into a :class:`datetime.time`, or ``None``."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, TypeError):
        return None


def normalize_prompt(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a prompt-schedule config, returning ``(clean, None)`` or ``(None, error)``.

    ``days`` are weekday ints (0=Mon … 6=Sun; de-duped and sorted), ``time`` is
    ``"HH:MM"`` 24-hour, ``enabled`` a bool, and ``question`` an optional custom
    line (whitespace-collapsed, length-capped). An enabled schedule with no days
    is rejected — that would silently never fire.
    """
    if not isinstance(raw, dict):
        return None, "prompt must be an object"
    days_raw = raw.get("days", [])
    if not isinstance(days_raw, list):
        return None, "days must be a list of weekday numbers (0=Mon … 6=Sun)"
    days = sorted(
        {int(d) for d in days_raw if isinstance(d, (int, float)) and 0 <= int(d) <= 6}
    )
    parsed_time = _parse_hhmm(raw.get("time"))
    if parsed_time is None:
        return None, "time must be 'HH:MM' (24-hour), e.g. '19:30'"
    enabled = bool(raw.get("enabled", True))
    if enabled and not days:
        return None, "pick at least one day, or disable the reminder"
    question = raw.get("question")
    if question is not None:
        question = re.sub(r"\s+", " ", str(question).strip())[:140] or None
    return {
        "enabled": enabled,
        "days": days,
        "time": parsed_time.strftime("%H:%M"),
        "question": question,
    }, None


def prompt_due(
    structured: Any,
    *,
    now_local: datetime,
    last_prompted_local: datetime | None = None,
) -> bool:
    """Whether a chart's award prompt should fire at ``now_local``.

    Fires on the first sweep at or after the configured time on a chosen weekday,
    then holds for the rest of that local day (``last_prompted_local`` on the same
    date suppresses it) — so a check that runs every 15–30 min sends exactly once.
    """
    p = structured.get("prompt") if isinstance(structured, dict) else None
    if not isinstance(p, dict) or not p.get("enabled"):
        return False
    if now_local.weekday() not in (p.get("days") or []):
        return False
    due_t = _parse_hhmm(p.get("time"))
    if due_t is None or now_local.time() < due_t:
        return False
    if last_prompted_local is not None and last_prompted_local.date() == now_local.date():
        return False
    return True


def prompt_question(structured: Any, child_name: str | None, title: str) -> str:
    """The prompt's question line — the chart's custom text, or a sensible default."""
    p = structured.get("prompt") if isinstance(structured, dict) else None
    custom = p.get("question") if isinstance(p, dict) else None
    if custom:
        return custom
    who = child_name or "the kids"
    return f"🌟 Did {who} earn a star for {title} today?"


# --- weekly mental-load check-in (pure) --------------------------------------
#
# Opt-in, household-scoped. Once a week both parents get a warm, non-judgmental
# "how did the invisible load feel for *you*?" self-report (light / balanced /
# heavy — three, since ntfy renders at most three action buttons). Once both have
# replied, a gentle shared note goes back to both — affirming if aligned, a soft
# "might be worth a chat" if one felt heavier, and it never names who. The
# schedule + last-sent live on the household row; the answers in household_checkins.

#: The self-report values, warm labels, and the one-tap actions that set them.
#: Kept in one place so notify.py buttons, the /nudge/act handler, and the store
#: never disagree on the vocabulary.
CHECKIN_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("light", "load_light", "Felt light 🙂"),
    ("balanced", "load_balanced", "Balanced ⚖️"),
    ("heavy", "load_heavy", "Carried a lot 🫠"),
)
#: One-tap action name → stored response value.
CHECKIN_ACTION_RESPONSE: dict[str, str] = {a: v for v, a, _ in CHECKIN_CHOICES}


def week_key(dt: datetime) -> str:
    """A stable ISO week key like ``"2026-W27"`` — the dedup unit for the check-in."""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def normalize_checkin_config(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the check-in schedule, returning ``(clean, None)`` or ``(None, error)``.

    ``day`` is a weekday int (0=Mon … 6=Sun) and ``time`` is ``"HH:MM"``. Enabling
    the check-in without both is rejected (it would never fire); a disabled config
    may omit them (turning it off).
    """
    if not isinstance(raw, dict):
        return None, "config must be an object"
    enabled = bool(raw.get("enabled", False))
    day = raw.get("day")
    if day is not None:
        try:
            day = int(day)
        except (ValueError, TypeError):
            return None, "day must be 0 (Mon) … 6 (Sun)"
        if not 0 <= day <= 6:
            return None, "day must be 0 (Mon) … 6 (Sun)"
    parsed_time = _parse_hhmm(raw.get("time"))
    if enabled and (day is None or parsed_time is None):
        return None, "pick a day and time, or turn the check-in off"
    return {
        "enabled": enabled,
        "day": day,
        "time": parsed_time.strftime("%H:%M") if parsed_time else None,
    }, None


def checkin_due(
    config: Any,
    *,
    now_local: datetime,
    last_sent_local: datetime | None = None,
) -> bool:
    """Whether the weekly check-in should fire now (once per ISO week, at/after its time)."""
    if not isinstance(config, dict) or not config.get("enabled"):
        return False
    day = config.get("day")
    if day is None or now_local.weekday() != day:
        return False
    due_t = _parse_hhmm(config.get("time"))
    if due_t is None or now_local.time() < due_t:
        return False
    if last_sent_local is not None and week_key(last_sent_local) == week_key(now_local):
        return False
    return True


def checkin_question() -> str:
    """The warm, welcoming ask — framed as *your* week, with no wrong answers."""
    return (
        "💛 Weekly check-in — no wrong answers, and it's not about keeping score. "
        "How has the invisible load felt for *you* this week?"
    )


def checkin_summary(responses: list[str]) -> str:
    """The gentle shared note once parents have replied — never names who felt what.

    Aligned/light → affirming; someone heavier → a soft invitation to talk (no
    scorekeeping); both heavy → a be-kind-to-each-other nudge. Kept deliberately
    non-accusatory so people stay honest.
    """
    heavy = sum(1 for r in responses if r == "heavy")
    both = len(responses) >= 2
    lead = "You both checked in 💛" if both else "Thanks for checking in 💛"
    if heavy == 0:
        return (
            f"{lead} Sounds like things feel pretty balanced right now — nice. "
            "Keep looking out for each other."
        )
    if heavy == len(responses):
        tail = " for both of you" if both else ""
        return (
            f"{lead} Sounds like a heavy stretch{tail}. Be extra gentle with each "
            "other this week — what's one thing you could drop or share?"
        )
    return (
        f"{lead} It sounds like it's felt heavier for one of you lately. "
        "No scorekeeping — maybe a good week to trade a task or two, or just talk it through."
    )


# --- daily delta digest (pure) -----------------------------------------------
#
# The active half of load-balancing (spec §7): push each parent the *other*
# parent's changes they haven't seen. "Seen" is when they last opened the sheet
# (household_seen_at); "digested" is when we last told them (household_digested_at).
# The diff below is filtered by both — so a parent is never told about their own
# edits, nor told twice — and stays silent when there's nothing new.

#: The once-a-day cap: a sweep can run more often, but a parent gets at most one
#: digest per this many hours.
MIN_DIGEST_INTERVAL_HOURS = 20


def unseen_changes(
    store: MemoryStore, *, viewer_id: int, since: str = ""
) -> list[dict[str, Any]]:
    """The other parent's sheet changes newer than ``since`` and not made by ``viewer_id``.

    Unions the provenance-carrying, household-shared writes — facts, agreements,
    and star grants — keeping only rows whose stored timestamp string sorts after
    ``since`` (``""`` = everything) and that the viewer did not author. Newest first.
    """
    out: list[dict[str, Any]] = []
    for f in store.facts():
        at = str(f.get("updated_at") or "")
        if f.get("updated_by") != viewer_id and at > since:
            out.append({"what": _fact_what(f), "who": f.get("updated_by_name"), "at": at})
    for a in store.agreements():
        at = str(a.get("updated_at") or "")
        if a.get("updated_by") != viewer_id and at > since:
            out.append(
                {"what": f"{a['title']} (plan)", "who": a.get("updated_by_name"), "at": at}
            )
    for g in store.recent_star_awards(limit=50):
        at = str(g.get("created_at") or "")
        if g.get("awarded_by") != viewer_id and at > since:
            out.append({"what": _star_grant_what(g), "who": g.get("awarded_by_name"), "at": at})
    out.sort(key=lambda c: c["at"], reverse=True)
    return out


def digest_message(changes: list[dict[str, Any]], *, max_items: int = 6) -> str:
    """A warm "here's what you missed" digest from the other parent's changes."""
    lines = ["💛 A few things your co-parent updated since you last looked:"]
    for c in changes[:max_items]:
        who = f" ({c['who']})" if c.get("who") else ""
        lines.append(f"• {c['what']}{who}")
    extra = len(changes) - min(len(changes), max_items)
    if extra > 0:
        lines.append(f"…and {extra} more on the sheet.")
    return "\n".join(lines)


def digest_interval_ok(
    last_digested: Any, now: datetime, *, min_hours: int = MIN_DIGEST_INTERVAL_HOURS
) -> bool:
    """Whether enough time has passed since the last digest (the once-a-day cap)."""
    if not last_digested:
        return True
    last = _parse_dt(last_digested)
    if last is None:
        return True
    return (now - last).total_seconds() >= min_hours * 3600


# --- load balance view (pure) ------------------------------------------------
#
# The *objective* companion to the subjective check-in: who's actually been
# keeping the sheet up, from provenance counts. Tone is the whole game — a raw
# "Dana 14, Alex 1" reads as a scoreboard, so the caption is gentle, names the
# carrier (not the slacker), and treats imbalance as a season, never a verdict.

#: How far back the balance view looks by default.
BALANCE_WINDOW_DAYS = 30

#: A split at/above this top share reads as lopsided enough to gently name.
_LOPSIDED_SHARE = 75


def balance_view(counts: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape per-member counts into shares + a gentle caption (no judgment)."""
    total = sum(c["count"] for c in counts)
    members = [
        {
            "name": c["name"],
            "count": c["count"],
            "share": round(100 * c["count"] / total) if total else 0,
        }
        for c in counts
    ]
    return {"total": total, "members": members, "caption": balance_caption(members, total)}


def balance_caption(members: list[dict[str, Any]], total: int) -> str:
    """A warm one-liner for the split — affirming when even, gentle when not."""
    if total == 0 or not members:
        return "No sheet updates yet — nothing to compare. 💛"
    top = max(members, key=lambda m: m["share"])
    if top["share"] < _LOPSIDED_SHARE:
        return "You've been sharing the sheet pretty evenly lately — nice teamwork. 💛"
    return (
        f"{top['name']} has been carrying most of the sheet lately. "
        "Some seasons just go that way — might be a nice week to trade a few things. "
        "No blame, just a gentle heads-up."
    )


# --- render ------------------------------------------------------------------


def _render_star_chart(
    structured: dict[str, Any],
    star_total: int = 0,
    next_g: dict[str, Any] | None = None,
) -> list[str]:
    """Render a star/points chart: a progress line, then thresholds → rewards."""
    thresholds = _star_thresholds(structured)
    if not thresholds:
        return []
    unit = structured.get("unit", "star")
    lines = []
    if star_total or next_g:
        head = f"  - **{star_total} {unit}s so far**"
        if next_g:
            head += f" · {next_g['remaining']} to go → {next_g['reward']}"
        else:
            head += " · all rewards earned! 🎉"
        lines.append(head)
    for n, reward in thresholds:
        lines.append(f"  - {n} {unit}s → {reward}")
    if structured.get("earn_only"):
        lines.append("  - _(earn-only — stars are never taken away)_")
    return lines


def render_sheet(sheet: HouseholdSheet) -> str:
    """Render a :class:`HouseholdSheet` as a single Markdown section.

    Deterministic and server-side — meant to drop into the existing ``/family``
    view rather than a new UI. Leads with what changed recently (the load
    surface), then the per-child grid, standing agreements, and upcoming appts.

    Args:
        sheet: The structured sheet from :func:`build_sheet`.

    Returns:
        A Markdown string.
    """
    title = sheet.household_name or "Household"
    lines = [f"# {title} — shared sheet", ""]

    if not any(
        (sheet.recently_changed, sheet.per_child, sheet.agreements, sheet.upcoming)
    ):
        lines.append(
            "_Nothing here yet. Add the kids' sizes, routines, allergies, and "
            "plans in plain English — “Sam's shoe size is 13”, “add a dentist "
            "appt for Sam next Tuesday 3pm”._"
        )
        return "\n".join(lines).rstrip() + "\n"

    # 1. Recently changed — the load-balancing surface, first.
    if sheet.recently_changed:
        lines.append("## Recently changed")
        for c in sheet.recently_changed:
            who = f" · {c.who}" if c.who else ""
            when = f" · {c.when}" if c.when else ""
            lines.append(f"- {c.what}{who}{when}")
        lines.append("")

    # 2. Per-child facts.
    for block in sheet.per_child:
        lines.append(f"## {block['child_name']}")
        for cat in block["categories"]:
            lines.append(f"**{cat['label']}**")
            for it in cat["items"]:
                value = it.get("value") or "—"
                lines.append(f"- {it['item']}: {value}")
            lines.append("")

    # 3. Standing agreements.
    if sheet.agreements:
        lines.append("## Standing agreements")
        for a in sheet.agreements:
            who = f" — {a['child_name']}" if a.get("child_name") else ""
            lines.append(f"### {a['title']}{who} _({a['kind']})_")
            if a.get("body"):
                lines.append(a["body"])
            if isinstance(a.get("structured"), dict):
                lines.extend(
                    _render_star_chart(
                        a["structured"], a.get("star_total", 0), a.get("next_goal")
                    )
                )
            lines.append("")

    # 4. Upcoming appointments.
    if sheet.upcoming:
        lines.append("## Upcoming appointments")
        for appt in sheet.upcoming:
            where = f" · {appt.location}" if appt.location else ""
            lines.append(f"- **{appt.when}** — {appt.title}{where}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
