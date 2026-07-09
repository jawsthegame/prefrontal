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

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from prefrontal.clock import TS_FMT
from prefrontal.clock import parse_ts as _parse_dt
from prefrontal.commitments import KIND_CHILD
from prefrontal.household._util import _fact_what, _parse_hhmm, _star_grant_what

# The chore/routine engine now lives in a submodule (audit #408, god-unit
# split); re-export its public API so ``from prefrontal.household import ...``
# is unchanged. ``name as name`` marks these as intentional re-exports.
from prefrontal.household.chores import (
    AWAY_AUTODETECT_CAP_DAYS as AWAY_AUTODETECT_CAP_DAYS,
)
from prefrontal.household.chores import (
    AWAY_AUTODETECT_MIN_DAYS as AWAY_AUTODETECT_MIN_DAYS,
)
from prefrontal.household.chores import (
    AWAY_BEHAVIORS as AWAY_BEHAVIORS,
)
from prefrontal.household.chores import (
    DEFAULT_AWAY_BEHAVIOR as DEFAULT_AWAY_BEHAVIOR,
)
from prefrontal.household.chores import (
    DEFAULT_CHORE_REMIND_BEFORE_MINUTES as DEFAULT_CHORE_REMIND_BEFORE_MINUTES,
)
from prefrontal.household.chores import (
    MAX_CHORE_REMIND_BEFORE_MINUTES as MAX_CHORE_REMIND_BEFORE_MINUTES,
)
from prefrontal.household.chores import (
    ChoreDecision as ChoreDecision,
)
from prefrontal.household.chores import (
    apply_service_shift as apply_service_shift,
)
from prefrontal.household.chores import (
    away_covers as away_covers,
)
from prefrontal.household.chores import (
    capped_away_window as capped_away_window,
)
from prefrontal.household.chores import (
    chore_missed_cover_message as chore_missed_cover_message,
)
from prefrontal.household.chores import (
    chore_missed_owner_message as chore_missed_owner_message,
)
from prefrontal.household.chores import (
    chore_missed_partner_message as chore_missed_partner_message,
)
from prefrontal.household.chores import (
    chore_reminder_cover_message as chore_reminder_cover_message,
)
from prefrontal.household.chores import (
    chore_reminder_message as chore_reminder_message,
)
from prefrontal.household.chores import (
    chore_reminder_shift_message as chore_reminder_shift_message,
)
from prefrontal.household.chores import (
    describe_chore_days as describe_chore_days,
)
from prefrontal.household.chores import (
    describe_month_days as describe_month_days,
)
from prefrontal.household.chores import (
    describe_schedule as describe_schedule,
)
from prefrontal.household.chores import (
    effective_chore_schedule as effective_chore_schedule,
)
from prefrontal.household.chores import (
    fmt_time_12h as fmt_time_12h,
)
from prefrontal.household.chores import (
    format_chore_days as format_chore_days,
)
from prefrontal.household.chores import (
    format_month_days as format_month_days,
)
from prefrontal.household.chores import (
    log_chore_done_and_celebrate as log_chore_done_and_celebrate,
)
from prefrontal.household.chores import (
    miss_due as miss_due,
)
from prefrontal.household.chores import (
    normalize_chore as normalize_chore,
)
from prefrontal.household.chores import (
    normalize_routine as normalize_routine,
)
from prefrontal.household.chores import (
    parse_chore_days as parse_chore_days,
)
from prefrontal.household.chores import (
    parse_month_days as parse_month_days,
)
from prefrontal.household.chores import (
    reminder_due as reminder_due,
)
from prefrontal.household.chores import (
    resolve_chore_context as resolve_chore_context,
)
from prefrontal.household.chores import (
    routine_chore_status as routine_chore_status,
)
from prefrontal.household.chores import (
    routine_complete_message as routine_complete_message,
)
from prefrontal.household.chores import (
    routine_is_complete as routine_is_complete,
)
from prefrontal.household.chores import (
    run_chores_check as run_chores_check,
)
from prefrontal.household.chores import (
    scheduled_on as scheduled_on,
)
from prefrontal.household.chores import (
    service_week as service_week,
)
from prefrontal.household.chores import (
    with_effective_schedule as with_effective_schedule,
)

# Co-parent load-balancing (check-in / digest / balance) now lives in a
# submodule (audit #408, god-unit split); re-export its public API so
# ``from prefrontal.household import ...`` is unchanged.
from prefrontal.household.load import (
    BALANCE_WINDOW_DAYS as BALANCE_WINDOW_DAYS,
)
from prefrontal.household.load import (
    CHECKIN_ACTION_RESPONSE as CHECKIN_ACTION_RESPONSE,
)
from prefrontal.household.load import (
    CHECKIN_CHOICES as CHECKIN_CHOICES,
)
from prefrontal.household.load import (
    MIN_DIGEST_INTERVAL_HOURS as MIN_DIGEST_INTERVAL_HOURS,
)
from prefrontal.household.load import (
    balance_caption as balance_caption,
)
from prefrontal.household.load import (
    balance_view as balance_view,
)
from prefrontal.household.load import (
    carrying_caption as carrying_caption,
)
from prefrontal.household.load import (
    checkin_due as checkin_due,
)
from prefrontal.household.load import (
    checkin_question as checkin_question,
)
from prefrontal.household.load import (
    checkin_summary as checkin_summary,
)
from prefrontal.household.load import (
    digest_interval_ok as digest_interval_ok,
)
from prefrontal.household.load import (
    digest_message as digest_message,
)
from prefrontal.household.load import (
    normalize_checkin_config as normalize_checkin_config,
)
from prefrontal.household.load import (
    unseen_changes as unseen_changes,
)
from prefrontal.household.load import (
    week_key as week_key,
)
from prefrontal.household.stars import _resolve_child_name, _star_thresholds

# Star charts / rewards now live in a submodule (audit #408, god-unit split);
# re-export the public API (unchanged imports) and pull the two private
# helpers the sheet/render/sweeps still use.
from prefrontal.household.stars import (
    award_stars_and_notify as award_stars_and_notify,
)
from prefrontal.household.stars import (
    newly_reached_goals as newly_reached_goals,
)
from prefrontal.household.stars import (
    next_goal as next_goal,
)
from prefrontal.household.stars import (
    parse_star_tiers as parse_star_tiers,
)
from prefrontal.household.stars import (
    parse_structured as parse_structured,
)
from prefrontal.household.stars import (
    star_congrats_text as star_congrats_text,
)
from prefrontal.impact import utcnow
from prefrontal.memory.repos.household import (
    FACT_CATEGORIES,
    FACT_CATEGORY_LABELS,
    HOUSEHOLD_WIDE,
)
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import local_datetime

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
        children: Kid roster dicts (id/name/birthday).
        pets: Pet roster dicts (id/name/species/birthday) — same table as the
            kids, split out so the app surfaces them under their own section.
        recently_changed: Most-recent writes, newest first (the load surface).
        per_child: Ordered blocks ``{child_id, child_name, categories}`` where
            ``categories`` is ``[{category, label, items}]`` in
            :data:`FACT_CATEGORIES` order; the household-wide block (child_id 0)
            comes first when present.
        per_pet: The same per-member fact blocks for the pets (no household-wide
            block — that belongs to the kids/household section).
        agreements: Standing-plan dicts (title/kind/body/child_name/structured),
            each also carrying ``star_total`` (the chart's running ledger total)
            and ``next_goal`` (the nearest unreached reward, or ``None``).
        upcoming: Upcoming child appointments, soonest first.
        shopping: Shopping-list dicts (id/item/spec/where_to_buy/got/child_name/
            added_by_name), still-needed first.
        chores: Recurring-chore dicts (id/title/owner_name/due_time/days/month_days/
            impact/enabled) each carrying ``done_today`` for today's local date,
            enabled first.
        counts: ``{children, facts, agreements, upcoming, shopping, chores}`` for a
            summary line (``shopping`` is the still-needed count; ``chores`` counts
            the enabled ones).
    """

    household_name: str | None
    children: list[dict[str, Any]] = field(default_factory=list)
    pets: list[dict[str, Any]] = field(default_factory=list)
    recently_changed: list[Change] = field(default_factory=list)
    per_child: list[dict[str, Any]] = field(default_factory=list)
    per_pet: list[dict[str, Any]] = field(default_factory=list)
    agreements: list[dict[str, Any]] = field(default_factory=list)
    upcoming: list[Appointment] = field(default_factory=list)
    shopping: list[dict[str, Any]] = field(default_factory=list)
    chores: list[dict[str, Any]] = field(default_factory=list)
    routines: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


# --- timestamp helpers (mirrors panic.py) ------------------------------------


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




def _appt_when(start: datetime | None, now: datetime, tz: str) -> str:
    """Friendly "when" for an appointment ("today 3:00pm", "Thu 3:00pm", "in 5d").

    ``start``/``now`` are naive UTC; convert both to the household's local zone
    before formatting the clock time and computing the day delta, so an Eastern
    family's 3pm appointment doesn't read as 7pm (raw UTC) or slip a day.
    """
    if start is None:
        return ""
    local_start = local_datetime(start, tz)
    local_now = local_datetime(now, tz)
    time_str = local_start.strftime("%-I:%M%p").lower()
    days = (local_start.date() - local_now.date()).days
    if days == 0:
        return f"today {time_str}"
    if days == 1:
        return f"tomorrow {time_str}"
    if 0 < days < 7:
        return f"{local_start.strftime('%a')} {time_str}"
    return f"{local_start.strftime('%b %-d')} {time_str}"


def build_sheet(
    store: MemoryStore, *, now: datetime | None = None, timezone: str = "UTC"
) -> HouseholdSheet:
    """Assemble the structured household sheet from memory (deterministic).

    Args:
        store: A store **scoped to a user who is in a household** (its household
            methods resolve the shared scope). A user with no household will
            raise from the store's ``_household_id`` guard.
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).
        timezone: The household's local timezone, used to resolve "today" for each
            chore's ``done_today`` flag (defaults to UTC).

    Returns:
        A :class:`HouseholdSheet`.
    """
    now = now or utcnow()

    household = store.household()
    children = store.children()
    pets = store.pets()
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
    # 2b. Per-pet facts (meds, vet/groomer contacts, food) — same grid, no
    # household-wide block (that belongs to the kids/household section).
    per_pet = _group_facts_by_child(facts, pets, include_household_wide=False)

    # 3. Agreements — parse structured JSON + attach the chart's star progress.
    agreements_out = [_prepare_agreement(a, star_totals.get(a["id"], 0)) for a in agreements]

    # 4. Upcoming child appointments — the viewer's kind='child' commitments.
    upcoming = _upcoming_child_appts(store, now, timezone)

    # 5. Shared shopping list — still-needed first.
    shopping = store.shopping_items()

    # 6. Recurring shared chores — with today's local done/pending status, and the
    # routines that group them (each flagged done_today once all its chores are in).
    routines = store.routines()
    chores = _chores_with_status(store, now, timezone, routines)
    today_local = local_datetime(now, timezone).strftime("%Y-%m-%d")
    done_ids = store.chore_ids_done_on(today_local)
    for r in routines:
        done, total = routine_chore_status(r["id"], chores, done_ids)
        r["chores_done"] = done
        r["chores_total"] = total
        r["done_today"] = total > 0 and done == total

    counts = {
        "children": len(children),
        "pets": len(pets),
        "facts": len(facts),
        "agreements": len(agreements),
        "upcoming": len(upcoming),
        "shopping": sum(1 for s in shopping if not s.get("got")),
        "chores": sum(1 for c in chores if c.get("enabled")),
        "routines": sum(1 for r in routines if r.get("enabled")),
    }
    return HouseholdSheet(
        household_name=(household or {}).get("name"),
        children=children,
        pets=pets,
        recently_changed=recently_changed,
        per_child=per_child,
        per_pet=per_pet,
        agreements=agreements_out,
        upcoming=upcoming,
        shopping=shopping,
        chores=chores,
        routines=routines,
        counts=counts,
    )


def _chores_with_status(
    store: MemoryStore,
    now: datetime,
    timezone: str,
    routines: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Chore rows with ``done_today`` + the *effective* schedule (routine-inherited).

    ``effective_days``/``effective_due_time`` resolve a chore's routine-inherited
    schedule (see :func:`with_effective_schedule`), so a surface renders when a
    chore actually runs without re-deriving it. Enabled first, then by due time.
    """
    chores = store.chores()
    if not chores:
        return []
    today = local_datetime(now, timezone).strftime("%Y-%m-%d")
    done_ids = store.chore_ids_done_on(today)
    routines_by_id = {r["id"]: r for r in (routines or [])}
    out = []
    for c in chores:
        eff = with_effective_schedule(c, routines_by_id.get(c.get("routine_id")))
        out.append({
            **c,
            "done_today": c["id"] in done_ids,
            "effective_days": eff["days"],
            "effective_month_days": eff["month_days"],
            "effective_due_time": eff["due_time"],
        })
    out.sort(
        key=lambda c: (0 if c.get("enabled") else 1, c.get("effective_due_time") or "", c["title"])
    )
    return out


def _group_facts_by_child(
    facts: list[dict[str, Any]],
    children: list[dict[str, Any]],
    *,
    include_household_wide: bool = True,
) -> list[dict[str, Any]]:
    """Group facts into per-member blocks, each block's items ordered by category.

    Serves both the kids grid (with the household-wide block first) and the pets
    grid (``include_household_wide=False`` — household-wide facts belong to the
    kids/household section, not repeated under the pets).
    """
    names = {c["id"]: c["name"] for c in children}
    # Deterministic block order: household-wide (0) first when included, then
    # roster members by name.
    order = ([HOUSEHOLD_WIDE] if include_household_wide else []) + [c["id"] for c in children]
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


def _upcoming_child_appts(
    store: MemoryStore, now: datetime, tz: str
) -> list[Appointment]:
    """The viewer's near-window child appointments (``kind='child'`` commitments)."""
    horizon = now + timedelta(days=UPCOMING_APPT_DAYS)
    out: list[Appointment] = []
    # Pass ``now`` so the lower bound matches the horizon's clock — otherwise the
    # query filters by the real wall clock while we window by the injected ``now``.
    for c in store.upcoming_commitments(limit=100, now=now):
        if c.get("kind") != KIND_CHILD:
            continue
        start = _parse_dt(c.get("start_at"))
        if start is None or start > horizon:
            continue
        out.append(
            Appointment(
                title=c.get("title") or "appointment",
                when=_appt_when(start, now, tz),
                start_at=str(c.get("start_at")),
                location=c.get("location"),
            )
        )
    return out


# --- scheduled award prompts (pure) ------------------------------------------
#
# A chart can carry a `prompt` block in its `structured` JSON — a recurring
# "should <kid> get a star for <chart> today?" check-in on chosen weekdays at a
# time of day. The schedule lives on the chart (no new table); the sweep that
# fires it (a periodic check endpoint / CLI) uses `prompt_due`, and the chart's
# `last_prompted_at` column dedups to once per day. Weekdays are Python's
# `date.weekday()` ints: Mon=0 … Sun=6.


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
        "time": parsed_time.strftime("%H:%M"),  # tz-ok: local wall-clock schedule from user input
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


# --- periodic household sweeps (one orchestration path per check) ------------
#
# Each of these is the single place a periodic household check runs, shared by
# its ``POST /webhooks/household/*/check`` endpoint and its ``prefrontal
# household *-check`` CLI twin — the same "one service, two thin callers" shape
# as run_chores_check / award_stars_and_notify. The service owns the domain
# decision (who's due, is this a shared household, has it fired this window) and
# the delivery; each caller only formats the returned data (JSON vs printed
# lines). Delivery failures never raise (the transport swallows them).


def run_star_prompt_sweep(
    store: MemoryStore,
    *,
    settings: Any,
    now: datetime | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Fire any star-award prompts due now, pushing both parents a one-tap ask.

    For each chart whose schedule is due it sends both parents a "did <kid> earn a
    star?" push and stamps ``last_prompted_at`` so it fires once per local day.

    Args:
        store: A store scoped to a household member.
        settings: Operator settings (timezone + one-tap signing origin/secret).
        now: Optional naive-UTC "now".
        client: Optional :class:`DeliveryClient` (tests inject a mock transport).

    Returns:
        ``{"sent": [{agreement_id, title, question, notified}], "checked_at": "%Y-%m-%d %H:%M"}``.
    """
    from prefrontal.integrations.delivery import (
        deliver_to_household,
        household_prompt_notice,
    )

    now = now or utcnow()
    now_local = local_datetime(now, settings.timezone)
    hid = store.household_id_or_none()
    sent: list[dict[str, Any]] = []
    for agreement in store.agreements():
        structured = parse_structured(agreement.get("structured"))
        last_dt = (
            _parse_dt(agreement["last_prompted_at"])
            if agreement.get("last_prompted_at") else None
        )
        last_local = local_datetime(last_dt, settings.timezone) if last_dt else None
        if not prompt_due(structured, now_local=now_local, last_prompted_local=last_local):
            continue
        child_name = _resolve_child_name(store, agreement.get("child_id") or 0)
        question = prompt_question(structured, child_name, agreement["title"])
        notified = deliver_to_household(
            store, hid,
            household_prompt_notice(question, agreement["id"], channel="push"),
            settings=settings, client=client,
            base_url=settings.oauth_base_url, secret=settings.session_secret,
        )
        store.mark_prompted(agreement["id"])
        sent.append(
            {"agreement_id": agreement["id"], "title": agreement["title"],
             "question": question, "notified": notified}
        )
    return {"sent": sent, "checked_at": now_local.strftime("%Y-%m-%d %H:%M")}


def run_checkin_sweep(
    store: MemoryStore,
    *,
    settings: Any,
    now: datetime | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Send the weekly mental-load check-in to both parents if it's due.

    Load-balancing is a two-parent concern, so a solo household is skipped. Fires
    once per ISO week (stamps ``checkin_last_sent_at``).

    Args:
        store: A store scoped to a household member.
        settings: Operator settings (timezone + one-tap signing origin/secret).
        now: Optional naive-UTC "now".
        client: Optional :class:`DeliveryClient` (tests inject a mock transport).

    Returns:
        ``{"sent": bool, "notified": [...], "checked_at": "%Y-%m-%d %H:%M",
        "reason": None | "not_shared" | "not_due"}`` — ``notified`` is populated
        only when ``sent`` is true.
    """
    from prefrontal.integrations.delivery import (
        deliver_to_household,
        household_checkin_notice,
    )

    now = now or utcnow()
    now_local = local_datetime(now, settings.timezone)
    checked_at = now_local.strftime("%Y-%m-%d %H:%M")
    if not store.is_shared_household():
        return {"sent": False, "notified": [], "checked_at": checked_at,
                "reason": "not_shared"}
    config = store.get_checkin_config()
    last_dt = (
        _parse_dt(config["last_sent_at"]) if config.get("last_sent_at") else None
    )
    last_local = local_datetime(last_dt, settings.timezone) if last_dt else None
    if not checkin_due(config, now_local=now_local, last_sent_local=last_local):
        return {"sent": False, "notified": [], "checked_at": checked_at,
                "reason": "not_due"}
    notified = deliver_to_household(
        store, store.household_id_or_none(),
        household_checkin_notice(checkin_question(), channel="push"),
        settings=settings, client=client,
        base_url=settings.oauth_base_url, secret=settings.session_secret,
    )
    store.mark_checkin_sent()
    return {"sent": True, "notified": notified, "checked_at": checked_at,
            "reason": None}


def run_digest_sweep(
    store: MemoryStore,
    *,
    settings: Any,
    now: datetime | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Push each parent the *other* parent's unseen sheet changes (self-suppressing).

    Per member, diff what the other parent changed since that member last looked
    (or was last digested), and — if there's anything new and it's been ~a day
    since their last digest — send just that member a catch-up. Silent when
    nothing's new; a solo or digest-off household is skipped.

    Args:
        store: A store scoped to a household member.
        settings: Operator settings (timezone + one-tap signing origin/secret).
        now: Optional naive-UTC "now".
        client: Optional :class:`DeliveryClient` (tests inject a mock transport).

    Returns:
        ``{"sent": [{handle, count, delivery}], "checked_at": <TS_FMT>,
        "reason": None | "not_shared" | "disabled"}``.
    """
    from prefrontal.integrations.delivery import (
        deliver_to_member,
        household_digest_notice,
    )

    now = now or utcnow()
    now_str = now.strftime(TS_FMT)
    if not store.is_shared_household():
        return {"sent": [], "checked_at": now_str, "reason": "not_shared"}
    if not store.get_digest_enabled():
        return {"sent": [], "checked_at": now_str, "reason": "disabled"}
    hid = store.household_id_or_none()
    sent: list[dict[str, Any]] = []
    for member in store.household_members(hid):
        if member.get("status") not in (None, "active"):
            continue
        m_store = store.scoped(member["id"])
        digested = m_store.get_state("household_digested_at")
        since = max(m_store.get_state("household_seen_at") or "", digested or "")
        changes = unseen_changes(m_store, viewer_id=member["id"], since=since)
        if not changes or not digest_interval_ok(digested, now):
            continue
        delivery = deliver_to_member(
            m_store,
            household_digest_notice(digest_message(changes), channel="push"),
            handle=member["handle"], settings=settings, client=client,
            base_url=settings.oauth_base_url, secret=settings.session_secret,
        )
        m_store.set_state("household_digested_at", now_str, source="inferred")
        sent.append(
            {"handle": member["handle"], "count": len(changes), "delivery": delivery}
        )
    return {"sent": sent, "checked_at": now_str, "reason": None}


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
        (
            sheet.recently_changed,
            sheet.per_child,
            sheet.pets,
            sheet.agreements,
            sheet.upcoming,
            sheet.shopping,
            sheet.chores,
            sheet.routines,
        )
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

    # 2c. Pets — the same fact grid (meds, vet/groomer, food) under their own
    # heading, so a dog's heartworm schedule doesn't hide among the kids' sizes.
    if sheet.pets:
        blocks_by_id = {b["child_id"]: b for b in sheet.per_pet}
        lines.append("## 🐾 Pets")
        for p in sheet.pets:
            species = f" · {p['species']}" if p.get("species") else ""
            lines.append(f"### {p['name']}{species}")
            block = blocks_by_id.get(p["id"])
            for cat in (block["categories"] if block else []):
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

    # 5. Shopping — still-needed as unchecked boxes, bought ones ticked.
    if sheet.shopping:
        lines.append("## Shopping")
        for s in sheet.shopping:
            box = "x" if s.get("got") else " "
            detail = " · ".join(
                p for p in (s.get("spec"), s.get("where_to_buy")) if p
            )
            who = f" — {s['child_name']}" if s.get("child_name") else ""
            tail = f" ({detail})" if detail else ""
            lines.append(f"- [{box}] {s['item']}{tail}{who}")
        lines.append("")

    # 6. Routines — a grouping + its accountable owner (the mental-load holder).
    if sheet.routines:
        lines.append("## Routines")
        for r in sheet.routines:
            holder = r.get("accountable_name") or "unassigned"
            bits = [f"accountable: {holder}", describe_schedule(r.get("days"), r.get("month_days"))]
            bits.append(f"by {fmt_time_12h(r['due_time'])}" if r.get("due_time") else "no set time")
            total = r.get("chores_total") or 0
            if r.get("done_today"):
                bits.append("✅ all done today!")
            elif total:
                bits.append(f"{r.get('chores_done', 0)}/{total} done")
            if not r.get("enabled"):
                bits.append("paused")
            impact = f" — {r['impact']}" if r.get("impact") else ""
            done_mark = "🎉 " if r.get("done_today") else ""
            lines.append(f"- {done_mark}**{r['title']}** ({' · '.join(bits)}){impact}")
        lines.append("")

    # 7. Shared chores — today's status, whose job, when it's due (effective), why.
    if sheet.chores:
        lines.append("## Shared chores")
        for c in sheet.chores:
            box = "x" if c.get("done_today") else " "
            owner = c.get("owner_name") or "either"
            due = c.get("effective_due_time", c.get("due_time"))
            bits = [
                f"{owner}",
                describe_schedule(
                    c.get("effective_days", c.get("days")),
                    c.get("effective_month_days", c.get("month_days")),
                ),
                f"by {fmt_time_12h(due)}" if due else "untimed",
            ]
            if c.get("routine_title"):
                bits.append(c["routine_title"])
            if not c.get("enabled"):
                bits.append("paused")
            meta = " · ".join(bits)
            impact = f" — {c['impact']}" if c.get("impact") else ""
            lines.append(f"- [{box}] {c['title']} ({meta}){impact}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


