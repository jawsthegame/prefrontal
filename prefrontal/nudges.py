"""Apply a one-tap nudge action — the domain half of ``GET /nudge/act``.

An ntfy ``http`` action button fires a signed background GET; the router
(:mod:`prefrontal.webhooks.routers.anchor`) verifies that signature, resolves +
scopes the user, and — after this returns — pushes the confirmation back and
renders the (unseen) HTML page. Everything a tapped button *does* lives here:
closing focus sessions / outings, logging departure and panic-step outcomes,
resolving a reflective pause, filing a trip's life-domain, awarding a star,
recording a weekly check-in, marking a chore done, confirming an away window,
and the self-care confirm/snooze — each reusing the same close/record helpers as
its dedicated endpoint rather than re-implementing them inline.

Splitting this out of the router (where it was a ~260-line handler dispatching
~18 actions) keeps the HTTP layer to ``verify → scope → apply → ack`` and gives
the one-tap path a single, testable home. Every branch is idempotent: re-tapping
a spent button returns a friendly headline rather than raising.
"""

from __future__ import annotations

from typing import Any

from prefrontal.briefing import record_briefing_feedback
from prefrontal.clock import TS_FMT, local_datetime
from prefrontal.coaching import resolve_ack
from prefrontal.config import Settings
from prefrontal.focus_balance import FOCUS_DOMAINS
from prefrontal.household import (
    CHECKIN_ACTION_RESPONSE,
    award_stars_and_notify,
    capped_away_window,
    checkin_summary,
    log_chore_done_and_celebrate,
    week_key,
)
from prefrontal.impact import utcnow
from prefrontal.log import get_logger
from prefrontal.memory.store import MemoryStore
from prefrontal.modules.hyperfocus import record_focus_end, record_focus_switched
from prefrontal.modules.location_anchor import (
    record_outing_abandoned,
    record_outing_return,
)
from prefrontal.modules.self_care import SELF_CARE_ACTIONS, apply_self_care_action
from prefrontal.panic import resolve_panic_step

logger = get_logger(__name__)

#: One-tap ``/nudge/act`` action → the coaching ``context_key`` its target lives
#: under, so a tap can resolve the delivered nudge's channel outcome (spec §8).
#: Actions absent here (the ``switch_*`` pause resolutions) aren't coaching cues,
#: so they map to no context and :func:`resolve_ack` no-ops.
_ACT_CONTEXT = {
    "focus_end": "focus",
    "outing_return": "outing",
    "outing_abandon": "outing",
    "made_it": "departure",
    "missed_it": "departure",
    # One-tap domain filing from the trip-label ask (all resolve the "trip" cue).
    **{f"trip_domain_{d}": "trip" for d in FOCUS_DOMAINS},
}

#: One-tap ``/nudge/act`` action → the *feature* it engages, for the usage loop's
#: ``engaged`` half. Keyed to module registry keys where one exists (so a feature's
#: ``offered`` and ``engaged`` counts line up on the /stats panel), and to a
#: pull-surface name (``panic``/``briefing``/``household``) otherwise. Every tap
#: flows through :func:`apply_nudge_action`, so one lookup here covers them all;
#: the raw ``action`` rides along as the ``intervention`` for finer slicing.
_ACT_FEATURE = {
    "focus_end": "hyperfocus",
    "switch_return": "impulsivity",
    "switch_defer": "impulsivity",
    "switch_switch": "impulsivity",
    "outing_return": "location_anchor",
    "outing_abandon": "location_anchor",
    "panic_step_done": "panic",
    "star_award": "household",
    "star_skip": "household",
    "digest_seen": "household",
    "chore_done": "household",
    "away_confirm": "household",
    "briefing_helped": "briefing",
    "briefing_not_helped": "briefing",
    "made_it": "departure",
    "missed_it": "departure",
    **{f"trip_domain_{d}": "trip_tracking" for d in FOCUS_DOMAINS},
    **{a: "self_care" for a in SELF_CARE_ACTIONS},
    **{a: "household" for a in CHECKIN_ACTION_RESPONSE},
}


def _record_engaged(memory: MemoryStore, action: str, target_id: int) -> None:
    """Best-effort ``engaged`` stamp for a one-tap action (never raises).

    A tap is the clearest signal a feature is *used*, so every *recognized* action
    that flows through :func:`apply_nudge_action` records one usage event. An
    unknown action (which the dispatcher below fails safe on) is deliberately
    *not* recorded — counting it would pollute the stats with a bogus feature.
    Telemetry must never break the button, so a failure is logged and swallowed —
    the tap's real work (below) is what matters.
    """
    feature = _ACT_FEATURE.get(action)
    if feature is None:
        return  # unrecognized action → no-op below, so nothing to attribute
    try:
        memory.record_feature_event(
            feature,
            "engaged",
            intervention=action,
            source="ntfy",
            ref=str(target_id),
        )
    except Exception:  # noqa: BLE001 — telemetry is best-effort, never fatal
        logger.debug("record_feature_event(engaged) failed for %r", action, exc_info=True)


def apply_nudge_action(
    memory: MemoryStore,
    action: str,
    target_id: int,
    *,
    user: dict[str, Any],
    settings: Settings,
) -> str:
    """Apply a verified one-tap nudge ``action`` and return the confirmation headline.

    Args:
        memory: A store already scoped to the tapping user.
        action: The verified action name (from the signed link).
        target_id: The verified target id (an entity id, or a synthetic ``0`` for
            actions that act on "now"/"today").
        user: The resolved (active) user row the signed link names.
        settings: Operator settings (timezone + one-tap signing origin/secret).

    Returns:
        A one-line confirmation the caller pushes back and renders. Idempotent:
        a spent button yields a friendly "already done" line, never an error.
    """
    # A tap is an acknowledgement: if the coaching agent delivered this nudge,
    # record which channel landed (feeds channel_response; spec §8). A no-op for
    # nudges the engine didn't originate.
    resolve_ack(memory, _ACT_CONTEXT.get(action, ""), target_id)

    # A tap is also the clearest "I use this feature" signal — the engaged half of
    # the usage feedback loop (best-effort; never blocks the button).
    _record_engaged(memory, action, target_id)

    if action == "focus_end":
        closed = memory.close_focus_session(target_id, status="ended")
        if closed is None:
            return "That focus session is already wrapped up."
        record_focus_end(memory, closed, outcome=None, breadcrumb=None)
        label = closed.get("intended_task") or "your session"
        return f"Wrapped up “{label}.” Nice work."

    if action in ("outing_return", "outing_abandon"):
        status_ = "returned" if action == "outing_return" else "abandoned"
        closed = memory.close_outing(target_id, status=status_)
        if closed is None:
            return "That outing is already closed."
        if status_ == "returned":
            record_outing_return(memory, closed)
            return "Welcome back — outing logged."
        record_outing_abandoned(memory, closed)
        return "Outing closed. No worries."

    if action.startswith("trip_domain_"):
        # One-tap file a just-finished trip into a life-sphere (focus balance).
        # target_id is the trip id; the domain is the action suffix.
        domain = action[len("trip_domain_") :]
        if domain not in FOCUS_DOMAINS:
            return "That trip filing option is no longer available."
        updated = memory.set_trip_domain(target_id, domain)
        if updated is None:
            return "That trip is no longer available."
        return f"Filed under {domain}. Thanks — that keeps your balance honest."

    if action in ("switch_return", "switch_defer", "switch_switch"):
        # Resolve a reflective pause in one tap (mirrors /webhooks/focus/resolve;
        # the impulse was already counted when the pause fired). The buttons ride
        # on the SwitchPause response, so the session is the pull's target.
        session = memory.get_focus_session(target_id)
        if session is None or session.get("status") != "active":
            return "That focus block is already resolved."
        task = session.get("intended_task") or "your task"
        if action == "switch_return":
            return f"Staying on “{task}.” 👊"
        if action == "switch_defer":
            memory.add_todo(
                f"Come back to what pulled you off “{task}”", source="impulse"
            )
            memory.mark_switch_deferred(target_id)
            return (
                "Parked it as a todo — back to what you were doing. "
                "(Rename it later if you like.)"
            )
        closed = memory.close_focus_session(target_id, status="switched")
        if closed is not None:
            record_focus_switched(memory, closed)
        return f"Switched away from “{task}.” Logged it."

    if action == "panic_step_done":
        # target_id is the pending panic episode logged when the nudge fired.
        done = resolve_panic_step(memory, target_id)
        if not done:
            return "Already logged — nice work either way."
        return "Logged — you took the first step. 👏 That's the hard part."

    if action in SELF_CARE_ACTIONS:
        # A self-care check has no entity id — it acts on "now"/"today", so we
        # recompute the local date at tap time rather than trust the synthetic
        # target. apply_self_care_action owns the confirm/snooze semantics.
        now = utcnow()
        today = local_datetime(now, settings.timezone).strftime("%Y-%m-%d")
        headline = apply_self_care_action(memory, action, now=now, today=today)
        return headline or "Done."

    if action == "star_award":
        # target_id is the star-chart agreement id. Award one star, attributed to
        # whoever tapped; a crossed reward goal congratulates both parents.
        result = award_stars_and_notify(
            memory, target_id, delta=1, awarded_by=user["id"],
            note="via prompt", settings=settings,
        )
        if result is None:
            return "That star chart is no longer available."
        who = result["child_name"] or "the kids"
        line = f"⭐ Star added for {who} — {result['total']} total."
        if result["goals_reached"]:
            line += " 🎉 Reward unlocked!"
        return line

    if action == "star_skip":
        return "No star today — that's okay. 🙂"

    if action in CHECKIN_ACTION_RESPONSE:
        # Weekly mental-load check-in: record how *this* parent's week felt (no
        # entity id — resolve the ISO week at tap time). Once everyone has replied,
        # send both parents the gentle shared note.
        from prefrontal.integrations.delivery import (
            deliver_to_household,
            household_notice,
        )

        now_local = local_datetime(utcnow(), settings.timezone)
        week = week_key(now_local)
        memory.record_checkin_response(
            week=week, user_id=user["id"], response=CHECKIN_ACTION_RESPONSE[action]
        )
        hid = memory.household_id_or_none()
        members = [
            m for m in memory.household_members(hid)
            if m.get("status") in (None, "active")
        ] if hid is not None else []
        responses = memory.checkin_responses(week) if hid is not None else []
        if members and len(responses) >= len(members):
            note = checkin_summary([r["response"] for r in responses])
            deliver_to_household(
                memory, hid, household_notice(note, channel="push"), settings=settings
            )
        return "Thanks for checking in 💛 That's really helpful."

    if action == "digest_seen":
        # Mark the shared sheet seen "now" so the delta digest won't re-surface
        # these changes — the one-tap equivalent of opening /kids.
        now = utcnow()
        memory.set_state("household_seen_at", now.strftime(TS_FMT), source="inferred")
        return "All caught up 💛"

    if action == "chore_done":
        # target_id is the chore id. Log it done for today (local date), attributed
        # to whoever tapped — so a partner picking up a slipped chore closes the
        # loop and stops the miss-handoff re-firing.
        today = local_datetime(utcnow(), settings.timezone).strftime("%Y-%m-%d")
        result = log_chore_done_and_celebrate(
            memory, chore_id=target_id, done_on=today, done_by=user["id"],
            settings=settings,
        )
        if result is None:
            return "That chore is no longer on the list."
        # Finishing a routine's last chore also congratulates both parents.
        done = result.get("routine_completed")
        if done:
            return f"Done — that wraps up “{done['title']}” for today! 🎉"
        return f"Done — “{result['title']}” is sorted for today. 🙌"

    if action == "away_confirm":
        # The multi-day-absence proposal was accepted: mark this member away so
        # their chores reassign to the present co-parent. Recompute the window from
        # the still-open trip at tap time — if they've since returned home there's
        # no open trip, so it's a friendly no-op rather than marking a member who's
        # back home as away.
        from datetime import timedelta

        trip = memory.active_trip()
        if trip is None:
            return "Looks like you're back home — nothing to mark. 🙂"
        now_local = local_datetime(utcnow(), settings.timezone)
        departed_local = now_local - timedelta(minutes=trip.get("elapsed_minutes") or 0)
        window = capped_away_window(
            departed_local.strftime("%Y-%m-%d"), now_local.strftime("%Y-%m-%d")
        )
        memory.set_member_away(**window)
        return (
            f"Marked you away through {window['ends_on']} — your chores will fall "
            "to your co-parent while you're gone. 💛 (Tap it away anytime you're back.)"
        )

    if action in ("briefing_helped", "briefing_not_helped"):
        # The morning briefing has no entity id — the vote is about "the digest you
        # just read" (target rides a synthetic 0). Record it; the tally steers the
        # LLM briefing voice via learned_briefing_guidance.
        helped = action == "briefing_helped"
        record_briefing_feedback(memory, helpful=helped)
        return (
            "Glad it helped 💛 I'll keep them like this." if helped
            else "Thanks — I'll make the next one tighter and more focused."
        )

    if action in ("made_it", "missed_it"):
        # Log a commitment's departure outcome.
        commitment = memory.get_commitment(target_id)
        title = (commitment or {}).get("title") or "your commitment"
        made = action == "made_it"
        memory.log_episode(
            "departure",
            acknowledged=True,
            context=f"commitment: {title}",
            outcome="success" if made else "miss",
            notes=f"one-tap: {'made it' if made else 'missed it'}",
        )
        memory.dismiss_departure(target_id)
        return (
            f"Logged — you made it to “{title}.” 🎯" if made
            else f"Logged — ran late for “{title}.” It happens."
        )

    # Unknown/unhandled action: fail safe. Previously this fell through the
    # made_it/missed_it branch as an unguarded else, so a malformed or
    # future action name silently logged a spurious "departure" miss and
    # dismissed a departure. Now it's a friendly no-op.
    logger.warning("apply_nudge_action: unhandled action %r (target %s)", action, target_id)
    return "That button isn't something I recognize anymore."
