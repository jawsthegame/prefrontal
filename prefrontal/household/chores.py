"""Recurring shared chores + routine completion (household-scoped).

Extracted from the ``household`` god-module (audit #408): the chore/routine
scheduling engine — recurrence + month-day parsing, the pure "should this fire
now?" timing predicates (reminder / miss / away / service-shift), the reminder
and miss-handoff message copy, the ``run_chores_check`` sweep, and routine
completion. Self-contained (no back-import from the package) so the sheet view
and the periodic sweeps in ``household/__init__.py`` depend on it, not the
reverse. Re-exported from :mod:`prefrontal.household`, so existing imports are
unchanged.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from prefrontal.clock import local_datetime
from prefrontal.household._util import _parse_hhmm
from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore

# --- recurring shared chores (pure) ------------------------------------------
#
# The active heart of shared load: a chore is one parent's recurring job (run
# the dishwasher, pack lunches) whose whole point is that forgetting it lands on
# the *other* parent. The notification flow reads a chore's schedule twice a day:
# a lead-time **reminder** to the owner before it's due, and — if the due time
# passes with the chore still undone — a gentle **miss-handoff** to the other
# parent so the slip isn't a morning surprise. The timing predicates below are
# pure (fed now/done/last-fired by the sweep), so "should this fire?" is
# unit-tested without a store or a clock, exactly like `prompt_due`/`checkin_due`.

#: Default lead time (minutes before the due time) for the owner's reminder.
DEFAULT_CHORE_REMIND_BEFORE_MINUTES = 30
#: Upper bound on the lead time (12h) — a reminder further out than that is a
#: config slip, not an intention, so we reject it rather than nudge at dawn.
MAX_CHORE_REMIND_BEFORE_MINUTES = 720

#: What a chore does while the household is "away" (vacation / travel). ``keep``
#: (the safe default) fires as normal — bills and meds don't pause for a trip.
#: ``suppress`` is a location-bound chore (trash, mail, plants) that simply can't
#: be done from afar, so it's silently skipped with a logged reason while away.
#: (``reassign`` is reserved for a later phase; not accepted yet.)
AWAY_BEHAVIORS: tuple[str, ...] = ("keep", "suppress")
DEFAULT_AWAY_BEHAVIOR = "keep"

#: How many days a member must be continuously away from home before the coach
#: *proposes* marking them away (their chores would then fall to the present
#: co-parent). A short overnight trip shouldn't trigger it; two days is the floor
#: where "someone else should cover my chores" starts to matter.
AWAY_AUTODETECT_MIN_DAYS = 2
#: Safety cap on an auto-detected away window's length. We can't know the return
#: date from a departure, so the confirmed window auto-expires after this many
#: days rather than lingering forever if the user forgets to clear it (returning
#: home before then is the normal end; this is just the backstop).
AWAY_AUTODETECT_CAP_DAYS = 14


def parse_chore_days(raw: Any) -> list[int]:
    """Parse a stored weekday CSV (``"0,2,4"``) into sorted ints, or ``[]`` (every day).

    Tolerant of whitespace and junk: only 0–6 survive, de-duped and sorted. An
    empty list is the "every day" sentinel (the schema's default ``days = ''``).
    """
    if isinstance(raw, (list, tuple, set)):
        parts: list[Any] = list(raw)
    else:
        parts = str(raw or "").split(",")
    days: set[int] = set()
    for p in parts:
        try:
            n = int(str(p).strip())
        except (ValueError, TypeError):
            continue
        if 0 <= n <= 6:
            days.add(n)
    return sorted(days)


def format_chore_days(days: Any) -> str:
    """Format a weekday list back to the stored CSV (``[0,2,4]`` → ``"0,2,4"``)."""
    return ",".join(str(d) for d in parse_chore_days(days))


def parse_month_days(raw: Any) -> list[int]:
    """Parse a stored day-of-month CSV (``"1,15"``) into sorted ints, or ``[]``.

    The month counterpart to :func:`parse_chore_days`: only 1–31 survive (junk and
    out-of-range dropped, de-duped, sorted). An empty list means "not month-scheduled"
    — the schedule falls back to :func:`parse_chore_days`' weekday behaviour.
    """
    if isinstance(raw, (list, tuple, set)):
        parts: list[Any] = list(raw)
    else:
        parts = str(raw or "").split(",")
    days: set[int] = set()
    for p in parts:
        try:
            n = int(str(p).strip())
        except (ValueError, TypeError):
            continue
        if 1 <= n <= 31:
            days.add(n)
    return sorted(days)


def format_month_days(days: Any) -> str:
    """Format a day-of-month list back to the stored CSV (``[15,1]`` → ``"1,15"``)."""
    return ",".join(str(d) for d in parse_month_days(days))


#: Short weekday labels for the human-readable schedule (Mon=0 … Sun=6).
_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _ordinal(n: int) -> str:
    """English ordinal for a day-of-month number (1 → "1st", 22 → "22nd", 31 → "31st")."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe_chore_days(days: Any) -> str:
    """A human schedule phrase: "every day", "weekdays", "weekends", or "Mon, Wed"."""
    parsed = parse_chore_days(days)
    if not parsed or parsed == list(range(7)):
        return "every day"
    if parsed == [0, 1, 2, 3, 4]:
        return "weekdays"
    if parsed == [5, 6]:
        return "weekends"
    return ", ".join(_WEEKDAY_LABELS[d] for d in parsed)


def describe_month_days(days: Any) -> str:
    """A human phrase for a day-of-month schedule: "the 1st & 15th", "" if none."""
    parsed = parse_month_days(days)
    if not parsed:
        return ""
    ordinals = [_ordinal(d) for d in parsed]
    if len(ordinals) == 1:
        joined = ordinals[0]
    else:
        joined = ", ".join(ordinals[:-1]) + " & " + ordinals[-1]
    return f"the {joined} of the month"


def describe_schedule(days: Any, month_days: Any = "") -> str:
    """The human schedule phrase, month-day-aware: month days win when set, else weekdays.

    Mirrors :func:`scheduled_on`'s precedence so a surface reads the same cadence the
    reminder sweep fires on — "the 1st & 15th of the month" when month days are set,
    otherwise the weekday phrase ("every day", "weekdays", "Mon, Wed", …).
    """
    md = describe_month_days(month_days)
    return md or describe_chore_days(days)


def fmt_time_12h(hhmm: Any) -> str:
    """Render a stored ``"HH:MM"`` as a friendly 12-hour time ("22:00" → "10:00pm")."""
    t = _parse_hhmm(hhmm)
    if t is None:
        return str(hhmm or "")
    # tz-ok: renders a stored local "HH:MM" schedule
    return datetime(2000, 1, 1, t.hour, t.minute).strftime("%-I:%M%p").lower()


def normalize_chore(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a chore definition, returning ``(clean, None)`` or ``(None, error)``.

    ``clean`` carries a storage-ready shape: ``title`` (trimmed, non-empty),
    ``owner_id`` (int or ``None`` = either parent), ``days`` as a weekday CSV
    (empty = every day), ``month_days`` as a day-of-month CSV (empty = not
    month-scheduled; when set it takes precedence over ``days``), ``due_time`` as
    ``"HH:MM"``, ``remind_before`` (1 … :data:`MAX_CHORE_REMIND_BEFORE_MINUTES`
    minutes), ``impact`` (collapsed, length-capped, or ``None``), and ``enabled``.
    Membership of ``owner_id`` is the caller's to check — that needs the store.
    """
    if not isinstance(raw, dict):
        return None, "chore must be an object"
    title = re.sub(r"\s+", " ", str(raw.get("title") or "").strip())
    if not title:
        return None, "title must be non-empty"
    # due_time is optional: blank means "inherit the routine's schedule, or run
    # untimed" (a checklist chore with no reminder). A non-blank value must parse.
    due_raw = raw.get("due_time")
    if due_raw in (None, ""):
        due_time = ""
    else:
        due = _parse_hhmm(due_raw)
        if due is None:
            return None, "due_time must be 'HH:MM' (24-hour), e.g. '22:00', or blank"
        due_time = due.strftime("%H:%M")  # tz-ok: normalizes a local schedule "HH:MM"
    days = format_chore_days(raw.get("days", []))
    month_days = format_month_days(raw.get("month_days", []))
    remind_before = raw.get("remind_before", DEFAULT_CHORE_REMIND_BEFORE_MINUTES)
    try:
        remind_before = int(remind_before)
    except (ValueError, TypeError):
        return None, "remind_before must be a whole number of minutes"
    if not 1 <= remind_before <= MAX_CHORE_REMIND_BEFORE_MINUTES:
        return None, f"remind_before must be 1–{MAX_CHORE_REMIND_BEFORE_MINUTES} minutes"
    owner_id = raw.get("owner_id")
    if owner_id is not None:
        try:
            owner_id = int(owner_id)
        except (ValueError, TypeError):
            return None, "owner_id must be a member's user id, or null for either parent"
    impact = raw.get("impact")
    if impact is not None:
        impact = re.sub(r"\s+", " ", str(impact).strip())[:160] or None
    away_behavior = raw.get("away_behavior", DEFAULT_AWAY_BEHAVIOR)
    if away_behavior in (None, ""):
        away_behavior = DEFAULT_AWAY_BEHAVIOR
    away_behavior = str(away_behavior).strip().lower()
    if away_behavior not in AWAY_BEHAVIORS:
        return None, f"away_behavior must be one of {', '.join(AWAY_BEHAVIORS)}"
    service = raw.get("service")
    if service is not None:
        # Free text (like a fact category), just normalized: lowercased, single-
        # spaced, capped. Blank → None (an ordinary, non-service chore).
        service = re.sub(r"\s+", " ", str(service).strip().lower())[:40] or None
    return {
        "title": title,
        "owner_id": owner_id,
        "days": days,
        "month_days": month_days,
        "due_time": due_time,
        "remind_before": remind_before,
        "impact": impact,
        "enabled": bool(raw.get("enabled", True)),
        "away_behavior": away_behavior,
        "service": service,
    }, None


def normalize_routine(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a routine definition, returning ``(clean, None)`` or ``(None, error)``.

    A routine groups chores and names one **accountable** owner (RACI "A" — the
    mental-load holder, distinct from the "responsible" doer of each chore). It
    carries the schedule its chores inherit: ``days`` (weekday CSV, empty = every
    day), ``month_days`` (day-of-month CSV, empty = not month-scheduled; when set it
    takes precedence over ``days``), and ``due_time`` (``"HH:MM"``, or empty = *not*
    time-tied — a plain grouping with no clock). ``accountable_id`` is an int member
    id or ``None`` (unassigned); membership is the caller's to check (needs the store).
    """
    if not isinstance(raw, dict):
        return None, "routine must be an object"
    title = re.sub(r"\s+", " ", str(raw.get("title") or "").strip())
    if not title:
        return None, "title must be non-empty"
    # due_time is optional for a routine: blank means "not time-tied".
    due_raw = raw.get("due_time")
    if due_raw in (None, ""):
        due_time = ""
    else:
        due = _parse_hhmm(due_raw)
        if due is None:
            return None, "due_time must be 'HH:MM' (24-hour), e.g. '07:30', or blank"
        due_time = due.strftime("%H:%M")  # tz-ok: normalizes a local schedule "HH:MM"
    accountable_id = raw.get("accountable_id")
    if accountable_id is not None:
        try:
            accountable_id = int(accountable_id)
        except (ValueError, TypeError):
            return None, "accountable_id must be a member's user id, or null"
    impact = raw.get("impact")
    if impact is not None:
        impact = re.sub(r"\s+", " ", str(impact).strip())[:160] or None
    return {
        "title": title,
        "accountable_id": accountable_id,
        "days": format_chore_days(raw.get("days", [])),
        "month_days": format_month_days(raw.get("month_days", [])),
        "due_time": due_time,
        "impact": impact,
        "enabled": bool(raw.get("enabled", True)),
    }, None


def effective_chore_schedule(
    chore: dict[str, Any], routine: dict[str, Any] | None = None
) -> tuple[str, str, str]:
    """The ``(days, month_days, due_time)`` a chore actually runs on (schedule inheritance).

    A chore that sets its **own** ``due_time`` overrides fully (its own days /
    month days + time win). Otherwise it **inherits** its routine's whole schedule
    (weekdays, days-of-month, and time together). A standalone chore with no time
    of its own is *untimed* — it never fires a reminder or miss-handoff; it's a
    checklist item you mark done (which still counts toward the "doing" balance).
    """
    own_due = _parse_hhmm(chore.get("due_time"))
    if own_due is not None:
        return (
            format_chore_days(chore.get("days")),
            format_month_days(chore.get("month_days")),
            own_due.strftime("%H:%M"),  # tz-ok: local schedule "HH:MM"
        )
    if routine is not None:
        return (
            format_chore_days(routine.get("days")),
            format_month_days(routine.get("month_days")),
            str(routine.get("due_time") or ""),
        )
    return (
        format_chore_days(chore.get("days")),
        format_month_days(chore.get("month_days")),
        "",
    )


def with_effective_schedule(
    chore: dict[str, Any], routine: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A copy of ``chore`` with ``days``/``month_days``/``due_time`` resolved via its routine."""
    days, month_days, due_time = effective_chore_schedule(chore, routine)
    return {**chore, "days": days, "month_days": month_days, "due_time": due_time}


def chore_ids_scheduled_on(
    store: MemoryStore, now_local: datetime, routines: list[dict[str, Any]] | None = None
) -> set[int]:
    """Ids of chores whose *effective* schedule runs on ``now_local``'s local date.

    The store twin of the sheet's ``scheduled_today`` flag, for surfaces that need
    "which chores run on <that day>" for an arbitrary local date (e.g. the sheet's
    day selector back-filling yesterday). Schedule is a property of the calendar,
    not the pause state, so a paused chore scheduled that day is still included —
    pausing silences reminders, it doesn't move the chore off the day.
    """
    routines_by_id = {r["id"]: r for r in (routines if routines is not None else store.routines())}
    ids: set[int] = set()
    for c in store.chores():
        eff = with_effective_schedule(c, routines_by_id.get(c.get("routine_id")))
        if scheduled_on(eff["days"], eff["month_days"], now_local):
            ids.add(c["id"])
    return ids


def _month_day_matches(month_days: list[int], now_local: datetime) -> bool:
    """Whether ``now_local``'s day-of-month matches any chosen day (short-month clamped).

    A day past the current month's length (e.g. the 31st in February) fires on the
    *last* day instead of silently skipping — so "pay rent on the 31st" still lands
    at the end of a 28/30-day month.
    """
    last = calendar.monthrange(now_local.year, now_local.month)[1]
    return any(now_local.day == min(d, last) for d in month_days)


def scheduled_on(days: Any, month_days: Any, now_local: datetime) -> bool:
    """Whether a schedule runs on ``now_local`` — month days win when set, else weekdays.

    When ``month_days`` is non-empty the schedule is a monthly one (those calendar
    days, regardless of weekday). Otherwise it falls back to the weekday behaviour:
    the chosen weekdays, or every day when ``days`` is empty too.
    """
    md = parse_month_days(month_days)
    if md:
        return _month_day_matches(md, now_local)
    wd = parse_chore_days(days)
    return not wd or now_local.weekday() in wd


def _due_minute(chore: dict[str, Any]) -> int | None:
    """The chore's due time as a minute-of-day, or ``None`` if it can't be parsed."""
    due = _parse_hhmm(chore.get("due_time"))
    return due.hour * 60 + due.minute if due is not None else None


def reminder_due(
    chore: dict[str, Any],
    *,
    now_local: datetime,
    done_today: bool,
    last_reminded_on: str | None = None,
) -> bool:
    """Whether the owner's lead-time reminder should fire at ``now_local``.

    Fires once, on the first sweep inside the ``[due − remind_before, due)``
    window on a scheduled, still-undone day. Past the due time the reminder
    yields to :func:`miss_due` (so a long-down server jumps straight to the
    miss, never a stale "due soon"). ``last_reminded_on`` on today's date
    suppresses a repeat.
    """
    if not chore.get("enabled", True) or done_today:
        return False
    if not scheduled_on(chore.get("days"), chore.get("month_days"), now_local):
        return False
    due_min = _due_minute(chore)
    if due_min is None:
        return False
    if last_reminded_on == now_local.strftime("%Y-%m-%d"):
        return False
    now_min = now_local.hour * 60 + now_local.minute
    remind_min = max(0, due_min - int(chore.get("remind_before") or 0))
    return remind_min <= now_min < due_min


def miss_due(
    chore: dict[str, Any],
    *,
    now_local: datetime,
    done_today: bool,
    last_missed_on: str | None = None,
) -> bool:
    """Whether the miss-handoff should fire — due time passed with the chore undone.

    Fires once per local day (``last_missed_on`` guards the repeat), on a
    scheduled day, at or after the due minute. This is the notification that
    reaches the *other* parent so a slip isn't a morning surprise.
    """
    if not chore.get("enabled", True) or done_today:
        return False
    if not scheduled_on(chore.get("days"), chore.get("month_days"), now_local):
        return False
    due_min = _due_minute(chore)
    if due_min is None:
        return False
    if last_missed_on == now_local.strftime("%Y-%m-%d"):
        return False
    now_min = now_local.hour * 60 + now_local.minute
    return now_min >= due_min


def away_covers(window: dict[str, Any] | None, now_local: datetime) -> bool:
    """Whether ``now_local``'s local date falls within an away ``window`` (inclusive).

    ``window`` is the ``{"starts_on", "ends_on", "note"}`` shape from
    :meth:`HouseholdRepo.away_window` (or ``None`` = not away). Dates are local
    ``"YYYY-MM-DD"`` strings, compared lexically — which is a correct chronological
    compare for that fixed format. A malformed or half-set window is treated as
    "not away" (fail-open: we'd rather nudge than silently swallow a real chore).
    """
    if not window:
        return False
    start = window.get("starts_on")
    end = window.get("ends_on")
    if not start or not end:
        return False
    today = now_local.strftime("%Y-%m-%d")  # tz-ok: local calendar date
    return start <= today <= end


def capped_away_window(
    starts_on: str, today: str, *, cap_days: int = AWAY_AUTODETECT_CAP_DAYS
) -> dict[str, str]:
    """The member-away window to write when an auto-detected absence is confirmed.

    Starts at the trip's departure date (``starts_on``, a local ``"YYYY-MM-DD"``)
    so a chore that already slipped while away is covered, and ends ``cap_days``
    after ``today`` — a backstop so a forgotten window can't suppress/reassign
    forever (the real end is returning home). Tagged so it's recognizable as a
    system-proposed window rather than one the user typed.
    """
    end = (
        datetime.strptime(today, "%Y-%m-%d") + timedelta(days=cap_days)
    ).strftime("%Y-%m-%d")  # tz-ok: local calendar arithmetic
    return {"starts_on": starts_on, "ends_on": end, "note": "auto-detected trip"}


def service_week(now_local: datetime) -> str:
    """The local Monday date (``"YYYY-MM-DD"``) of the week containing ``now_local``.

    Service shifts are keyed per calendar week, so this is the join key between a
    scraped shift and "which week are we in now". A shift for a past week simply
    stops matching once the week rolls over — no cleanup needed.
    """
    monday = now_local - timedelta(days=now_local.weekday())  # tz-ok: local week
    return monday.strftime("%Y-%m-%d")


def apply_service_shift(chore: dict[str, Any], shift: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``chore`` whose schedule is moved to this week's shifted pickup day.

    When a holiday bumps trash from Tue to Wed, the reminder should follow: we
    replace the chore's effective ``days`` with just the shifted weekday (and clear
    ``month_days``) so it fires on the new day and *not* the normal one — no wrong-day
    nudge. The original ``shift`` rides along under ``_service_shift`` so the reminder
    text can explain the move (:func:`chore_reminder_shift_message`).
    """
    return {
        **chore,
        "days": str(int(shift["shifted_weekday"])),
        "month_days": "",
        "_service_shift": shift,
    }


def chore_reminder_shift_message(chore: dict[str, Any]) -> str:
    """The owner's reminder on a shifted pickup day — names the moved day + why."""
    shift = chore.get("_service_shift") or {}
    day = _WEEKDAY_LABELS[int(shift.get("shifted_weekday", 0)) % 7]
    reason = shift.get("reason")
    why = f" ({reason})" if reason else ""
    return (
        f"🧼 {chore['title']} goes out tonight — pickup moved to {day} this week{why}, "
        f"by {fmt_time_12h(chore.get('due_time'))}. Tap Done once it's out."
    )


@dataclass(frozen=True)
class ChoreDecision:
    """A chore's context gate outcome for one sweep tick.

    ``action`` is ``"proceed"`` (let the normal reminder/miss predicates decide) or
    ``"suppress"`` (skip this chore today — it can't or shouldn't fire). ``reason``
    is a short human explanation, present only when suppressed, so the skip is
    legible in the sweep result rather than silent. (Later phases add ``"shift"`` /
    ``"reassign"``; this phase is proceed/suppress only.)
    """

    action: str
    reason: str = ""


def resolve_chore_context(
    chore: dict[str, Any],
    *,
    now_local: datetime,
    away_window: dict[str, Any] | None = None,
    all_members_away: bool = False,
) -> ChoreDecision:
    """Decide whether a chore should be suppressed by household context this tick.

    The single context gate that sits between "is this chore scheduled today?" and
    the timing predicates in :func:`run_chores_check`. It suppresses a chore marked
    ``away_behavior="suppress"`` (a location-bound chore — trash, mail, plants —
    that can't be done from afar) when nobody's home: either the household-wide
    **away window** is active, or **every** member has individually marked
    themselves away (``all_members_away``) — same effect, nobody to do it.
    Everything else proceeds normally, so bills and meds still fire on vacation.
    Only reports a suppression for a chore that is enabled and actually scheduled
    today, so the sweep result stays meaningful.
    """
    if not chore.get("enabled", True):
        return ChoreDecision("proceed")
    if not scheduled_on(chore.get("days"), chore.get("month_days"), now_local):
        return ChoreDecision("proceed")
    if chore.get("away_behavior") == "suppress":
        if away_covers(away_window, now_local):
            note = (away_window or {}).get("note")
            ends = (away_window or {}).get("ends_on")
            why = f" ({note})" if note else ""
            return ChoreDecision("suppress", f"household is away through {ends}{why}")
        if all_members_away:
            return ChoreDecision("suppress", "everyone is away")
    return ChoreDecision("proceed")


def _impact_clause(chore: dict[str, Any]) -> str:
    """" If it slips, <impact>." — the reason the chore matters, or "" if none set."""
    impact = chore.get("impact")
    return f" If it slips, {impact}." if impact else ""


def chore_reminder_message(chore: dict[str, Any]) -> str:
    """The owner's lead-time nudge — warm, with the "why" and a one-tap Done."""
    return (
        f"🧼 Time to {chore['title']} — ideally by {fmt_time_12h(chore.get('due_time'))}."
        f"{_impact_clause(chore)} Tap Done once it's sorted."
    )


def chore_missed_owner_message(chore: dict[str, Any]) -> str:
    """The still-not-done nudge to the owner once the due time has passed (no blame)."""
    return (
        f"🧼 “{chore['title']}” still isn't done — it was due by "
        f"{fmt_time_12h(chore.get('due_time'))}. No stress; do it when you can and tap Done."
    )


def chore_missed_partner_message(chore: dict[str, Any]) -> str:
    """The gentle heads-up to the *other* parent — so a slip isn't a morning surprise."""
    return (
        f"💛 Heads up — “{chore['title']}” didn't get done today (was due "
        f"{fmt_time_12h(chore.get('due_time'))}).{_impact_clause(chore)} "
        "Flagging it so it's not a morning surprise — no need to jump on it unless "
        "you want to. Tap Done if you pick it up."
    )


def chore_reminder_cover_message(chore: dict[str, Any], away_owner: str) -> str:
    """The reminder to the present partner when the owner is away — "can you cover?"."""
    return (
        f"🧼 {away_owner}'s away — can you {chore['title']} by "
        f"{fmt_time_12h(chore.get('due_time'))}?{_impact_clause(chore)} "
        "Tap Done once it's sorted."
    )


def chore_missed_cover_message(chore: dict[str, Any], away_owner: str) -> str:
    """The miss nudge to the present partner when the away owner's chore has slipped."""
    return (
        f"🧼 “{chore['title']}” still isn't done — it was {away_owner}'s, but they're "
        f"away (due by {fmt_time_12h(chore.get('due_time'))}). No stress; pick it up "
        "when you can and tap Done."
    )


def run_chores_check(
    store: MemoryStore,
    *,
    settings: Any,
    now: datetime | None = None,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Fire any chore reminders / miss-handoffs due now, notifying the right parent(s).

    The single orchestration path behind both ``POST /webhooks/household/chores/check``
    and ``prefrontal household chores-check`` (like :func:`award_stars_and_notify` is
    the one path behind every way a star is given). For each enabled, still-undone
    chore scheduled today it sends either the owner's lead-time **reminder** or —
    once the due time has passed — the owner a still-not-done nudge **and** the
    *other* parent a gentle **heads-up**. Each stage stamps its per-day cursor so a
    check running every 15–30 min fires each exactly once. Delivery failures never
    raise (each transport swallows them); returns one entry per chore that fired.

    Args:
        store: A store scoped to a household member.
        settings: Operator settings (timezone + one-tap signing origin/secret).
        now: Optional naive-UTC "now" (defaults to :func:`prefrontal.impact.utcnow`).
        client: Optional :class:`DeliveryClient` (tests inject a mock transport).
    """
    from prefrontal.integrations.delivery import (
        deliver_to_member,
        household_chore_notice,
    )

    now = now or utcnow()
    now_local = local_datetime(now, settings.timezone)
    today = now_local.strftime("%Y-%m-%d")
    hid = store.household_id_or_none()
    members = [
        m for m in store.household_members(hid) if m.get("status") in (None, "active")
    ]
    done_ids = store.chore_ids_done_on(today)
    # A chore inherits its routine's schedule unless it sets its own (see
    # with_effective_schedule); resolve once so the pure timing predicates below
    # see the effective days/due_time. An untimed chore never fires.
    routines_by_id = {r["id"]: r for r in store.routines()}
    # The household's away window (vacation / travel), if any — resolved once so
    # the per-chore context gate below can silently skip location-bound chores.
    away = store.away_window()
    # Per-member away state: a single member who's marked themselves away isn't
    # nudged; their chores fall to whoever's present. If *everyone* is away it's
    # the same as household-away (nobody to do it) — the gate suppresses then too.
    away_member_ids = {
        m["id"] for m in members
        if away_covers(store.scoped(m["id"]).member_away_window(), now_local)
    }
    present_members = [m for m in members if m["id"] not in away_member_ids]
    all_members_away = bool(members) and not present_members
    # Municipal service shifts apply per calendar week (e.g. a holiday bumped trash
    # to Wednesday this week). Resolve the week key once; a service-linked chore
    # below moves its reminder to the shifted day when a matching shift exists.
    week = service_week(now_local)
    deliver_kw = {
        "settings": settings,
        "client": client,
        "base_url": settings.oauth_base_url,
        "secret": settings.session_secret,
    }

    def _member_name(member: dict[str, Any]) -> str:
        return member.get("display_name") or member.get("handle") or "your co-parent"

    def _to_member(member: dict[str, Any], text: str, cid: int) -> dict[str, Any]:
        row = deliver_to_member(
            store.scoped(member["id"]), household_chore_notice(text, cid),
            handle=member["handle"], **deliver_kw,
        )
        return {"handle": member["handle"], "delivery": row}

    def _reminder_text(c: dict[str, Any]) -> str:
        """The owner/everyone reminder — rephrased when a service shift moved the day."""
        if c.get("_service_shift"):
            return chore_reminder_shift_message(c)
        return chore_reminder_message(c)

    sent: list[dict[str, Any]] = []
    for raw_chore in store.chores():
        chore = with_effective_schedule(
            raw_chore, routines_by_id.get(raw_chore.get("routine_id"))
        )
        # Service shift: if this chore tracks a municipal service (trash/recycling)
        # and a holiday moved this week's pickup, move the reminder to the shifted
        # day — so it fires then, not on the normal (now-wrong) day.
        service = raw_chore.get("service")
        if service:
            shift = store.service_shift(service, week)
            if shift:
                chore = apply_service_shift(chore, shift)
        # Context gate: an active away window skips location-bound chores. Skipping
        # (vs. stamping a cursor) means the chore resumes cleanly once we're back —
        # no stale "you missed it" waiting on the far side of the trip.
        decision = resolve_chore_context(
            chore, now_local=now_local, away_window=away,
            all_members_away=all_members_away,
        )
        if decision.action == "suppress":
            sent.append({"chore_id": chore["id"], "title": chore["title"],
                         "stage": "suppressed", "reason": decision.reason})
            continue
        cid = chore["id"]
        done = cid in done_ids
        owner_id = chore.get("owner_id")
        owner = next((m for m in members if m["id"] == owner_id), None)
        # An away owner (with someone still home) hands off: the present partner is
        # nudged to cover, and the away owner stays silent. If the owner is present —
        # or everyone is away, so there's no one to hand off to — normal routing.
        owner_away = owner is not None and owner["id"] in away_member_ids
        cover = owner_away and bool(present_members)
        if reminder_due(
            chore, now_local=now_local, done_today=done,
            last_reminded_on=chore.get("last_reminded_on"),
        ):
            if cover:
                text = chore_reminder_cover_message(chore, _member_name(owner))
                notified = [_to_member(m, text, cid) for m in present_members]
            elif owner is not None:  # owner present (or all away → they still get it)
                notified = [_to_member(owner, _reminder_text(chore), cid)]
            else:  # unassigned — everyone present owns it (all, if nobody's home)
                text = _reminder_text(chore)
                notified = [_to_member(m, text, cid) for m in (present_members or members)]
            store.mark_chore_reminded(cid, today)
            sent.append({"chore_id": cid, "title": chore["title"],
                         "stage": "reminder", "notified": notified})
        elif miss_due(
            chore, now_local=now_local, done_today=done,
            last_missed_on=chore.get("last_missed_on"),
        ):
            notified = []
            if cover:
                # The away owner's chore slipped: the present partner picks it up;
                # the away owner isn't nagged on their trip.
                text = chore_missed_cover_message(chore, _member_name(owner))
                notified = [_to_member(m, text, cid) for m in present_members]
            else:
                # Normal miss handoff, but an away member gets neither the owner
                # nudge nor the heads-up. All-away keep chore → notify everyone.
                for member in (present_members or members):
                    if owner_id is None or member["id"] == owner_id:
                        text = chore_missed_owner_message(chore)
                    else:
                        text = chore_missed_partner_message(chore)
                    notified.append(_to_member(member, text, cid))
            store.mark_chore_missed(cid, today)
            sent.append({"chore_id": cid, "title": chore["title"],
                         "stage": "missed", "notified": notified})
    return sent



# --- routine completion (pure + notify) --------------------------------------
#
# A routine groups chores; it's "done for the day" once every one of its enabled
# chores has been logged done today. Finishing that last chore is a small shared
# win worth naming — so the done-path (endpoint, one-tap, CLI) runs the check and,
# on the first completion of the day, congratulates *both* parents (like a star
# goal) and the sheet highlights the routine. The completion test is pure (fed the
# routine's chores + today's done-ids); a per-day cursor (`last_completed_on`)
# dedups the celebration to once per local day.


def routine_chore_status(
    routine_id: int, chores: list[dict[str, Any]], done_ids: set[int]
) -> tuple[int, int]:
    """``(done, total)`` for a routine's **enabled** linked chores given today's done-ids."""
    linked = [
        c for c in chores
        if c.get("routine_id") == routine_id and c.get("enabled", True)
    ]
    done = sum(1 for c in linked if c["id"] in done_ids)
    return done, len(linked)


def routine_is_complete(
    routine_id: int, chores: list[dict[str, Any]], done_ids: set[int]
) -> bool:
    """Whether every enabled chore under ``routine_id`` is done today.

    A routine with no enabled chores is never "complete" — there's nothing to
    finish, so it never fires a hollow celebration.
    """
    done, total = routine_chore_status(routine_id, chores, done_ids)
    return total > 0 and done == total


def routine_complete_message(routine: dict[str, Any], total: int) -> str:
    """The warm "you finished the whole routine" note sent to both parents."""
    title = routine.get("title") or "that routine"
    holder = routine.get("accountable_name")
    count = f"all {total} " if total > 1 else "the "
    tail = f" Nice work, {holder} 💛" if holder else " Nice teamwork 💛"
    return f"🎉 “{title}” is done for today — {count}task{'s' if total != 1 else ''} sorted.{tail}"


def log_chore_done_and_celebrate(
    store: MemoryStore,
    *,
    chore_id: int,
    done_on: str,
    done_by: int | None,
    settings: Any = None,
    client: Any = None,
) -> dict[str, Any] | None:
    """Log a chore done, then congratulate the household if it finished a routine.

    The single path behind every "mark done" surface (the HTTP endpoint, the
    one-tap button, the CLI) — mirroring :func:`award_stars_and_notify` as the one
    place the "did this cross a line? then tell both parents" rule lives. Logs the
    completion (idempotent), and if this chore's routine just had its **last**
    enabled chore finished for the first time today, delivers an encouraging notice
    to both parents and stamps the routine's per-day cursor so it fires once.

    Returns ``None`` when the chore isn't in the store's household (so the caller
    can 404), otherwise the ``log_chore_done`` result dict plus a
    ``routine_completed`` key: ``None``, or ``{routine_id, title, notified}``.
    """
    result = store.log_chore_done(chore_id=chore_id, done_on=done_on, done_by=done_by)
    if result is None:
        return None
    result["routine_completed"] = _celebrate_routine_if_complete(
        store, chore_id, done_on, settings=settings, client=client
    )
    return result


def _celebrate_routine_if_complete(
    store: MemoryStore,
    chore_id: int,
    today: str,
    *,
    settings: Any = None,
    client: Any = None,
) -> dict[str, Any] | None:
    """Notify + stamp if ``chore_id``'s routine is now fully done today (else ``None``)."""
    chore = store.chore(chore_id)
    routine_id = chore.get("routine_id") if chore else None
    if not routine_id:
        return None
    routine = store.routine(routine_id)
    if routine is None or not routine.get("enabled"):
        return None
    if routine.get("last_completed_on") == today:
        return None  # already celebrated today — don't re-fire on a re-tap
    done_ids = store.chore_ids_done_on(today)
    _, total = routine_chore_status(routine_id, store.chores(), done_ids)
    if not routine_is_complete(routine_id, store.chores(), done_ids):
        return None
    store.mark_routine_completed(routine_id, today)
    # Lazy import: delivery pulls in webhooks.notify (see award_stars_and_notify).
    from prefrontal.integrations.delivery import deliver_to_household, household_notice

    notified = deliver_to_household(
        store,
        store.household_id_or_none(),
        household_notice(routine_complete_message(routine, total), channel="sound"),
        settings=settings,
        client=client,
    )
    return {"routine_id": routine_id, "title": routine["title"], "notified": notified}


