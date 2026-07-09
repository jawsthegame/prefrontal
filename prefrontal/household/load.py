"""Co-parent load-balancing — weekly check-in, delta digest, balance view.

Extracted from the ``household`` god-module (audit #408): the three features that
make invisible load legible — the opt-in weekly mental-load **check-in**, the
**delta digest** of what the other parent changed since you last looked, and the
contribution / accountability **balance view**. Self-contained; re-exported from
:mod:`prefrontal.household` so existing imports are unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from prefrontal.clock import parse_ts as _parse_dt
from prefrontal.household._util import _fact_what, _parse_hhmm, _star_grant_what
from prefrontal.memory.store import MemoryStore

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
        "time": parsed_time.strftime("%H:%M") if parsed_time else None,  # tz-ok: local wall-clock schedule from user input
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


def _share_members(counts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Attach a percentage share to each member's count; return ``(members, total)``."""
    total = sum(c["count"] for c in counts)
    members = [
        {
            "name": c["name"],
            "count": c["count"],
            "share": round(100 * c["count"] / total) if total else 0,
        }
        for c in counts
    ]
    return members, total


def balance_view(
    counts: list[dict[str, Any]], *, carrying: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Shape per-member counts into shares + a gentle caption (no judgment).

    Two facets of shared load (learning/parent-pack): ``counts`` is the **doing**
    tally — sheet edits, star awards, and chores actually completed in the window.
    ``carrying`` (optional) is the **accountability** tally — how many active
    routines each parent holds the mental load for (RACI "A"), a standing count
    rather than a windowed one. Doing and carrying are reported separately because
    they're different kinds of load: knocking out ten chores isn't the same as
    being the one answerable for the whole bedtime routine.

    Returns the doing facet at the top level (``total``/``members``/``caption``,
    unchanged) plus, when ``carrying`` is given, a ``carrying`` block with its own
    members/shares and caption.
    """
    members, total = _share_members(counts)
    view: dict[str, Any] = {
        "total": total,
        "members": members,
        "caption": balance_caption(members, total),
    }
    if carrying is not None:
        c_members, c_total = _share_members(carrying)
        view["carrying"] = {
            "total": c_total,
            "members": c_members,
            "caption": carrying_caption(c_members, c_total),
        }
    return view


def balance_caption(members: list[dict[str, Any]], total: int) -> str:
    """A warm one-liner for the doing split — affirming when even, gentle when not."""
    if total == 0 or not members:
        return "No shared work logged yet — nothing to compare. 💛"
    top = max(members, key=lambda m: m["share"])
    if top["share"] < _LOPSIDED_SHARE:
        return "You've been sharing the load pretty evenly lately — nice teamwork. 💛"
    return (
        f"{top['name']} has been carrying most of the doing lately. "
        "Some seasons just go that way — might be a nice week to trade a few things. "
        "No blame, just a gentle heads-up."
    )


def carrying_caption(members: list[dict[str, Any]], total: int) -> str:
    """A warm one-liner for the accountability split (who holds the mental load)."""
    if total == 0 or not members:
        return "No routines have an owner yet — assigning one gives it a home. 💛"
    top = max(members, key=lambda m: m["share"])
    if top["share"] < _LOPSIDED_SHARE:
        return "The mental load of your routines is split pretty evenly — that's the goal. 💛"
    return (
        f"{top['name']} is holding the mental load for most of your routines. "
        "That invisible work adds up — worth seeing if one could change hands."
    )



