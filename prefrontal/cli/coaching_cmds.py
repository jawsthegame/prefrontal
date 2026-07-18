"""``prefrontal`` coaching-loop commands.

The proactive-loop surfaces: the morning ``briefing``, ``coach`` tick (+ native
delivery), ``panic`` triage, ``next`` thing, day-shape ``day``, ``encourage``,
``self-care``, ``focus``, ``open-day``, ``usage`` nudge, and ``notify``. Includes
the delivery-orchestration helpers (``_deliver_briefing`` / ``_deliver_decisions``
/ ``_deliver_panic`` / ``_coach_tick`` / ``_briefing_text``) these commands share.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prefrontal.briefing import build_briefing, render_briefing, summarize_briefing
from prefrontal.cli._common import _resolve_user_store, _user_targets
from prefrontal.coaching import build_context, collect_cues
from prefrontal.config import get_settings
from prefrontal.encouragement import (
    assess_day,
    build_recovery,
    encouragement_cues,
    render_encouragement,
    summarize_encouragement,
)
from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore
from prefrontal.modules import enabled_modules
from prefrontal.panic import build_panic, render_panic, summarize_panic


def register(sub) -> None:
    """Attach the coaching-loop subcommands."""
    p_brief = sub.add_parser("briefing", help="Print today's morning briefing.")
    p_brief.add_argument("--db-path", default=None, help="Override the database path.")
    p_brief.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_brief.add_argument(
        "--llm", action="store_true", help="Rewrite as prose via Ollama (falls back)."
    )
    p_brief.add_argument("-o", "--output", default=None, help="Write to a file instead of stdout.")
    p_brief.add_argument(
        "--deliver",
        action="store_true",
        help="Publish the briefing as a push (native morning-briefing delivery).",
    )
    p_brief.add_argument(
        "--all-users",
        action="store_true",
        help="With --deliver, brief every active user (one launchd job for the box).",
    )
    p_brief.add_argument(
        "--channel",
        default="push",
        choices=("digest", "push", "sound", "voice"),
        help="Delivery channel class for --deliver (default: push).",
    )
    p_brief.set_defaults(func=_cmd_briefing)

    p_focus = sub.add_parser(
        "focus", help="Arm a focus session from a live calendar block."
    )
    focus_sub = p_focus.add_subparsers(dest="focus_action", required=True)
    p_focus_arm = focus_sub.add_parser(
        "arm", help="Auto-start a session for a live focus/deep-work calendar block."
    )
    p_focus_arm.add_argument("--db-path", default=None, help="Override the database path.")
    p_focus_arm.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_focus_arm.add_argument(
        "--all-users", action="store_true", help="Fan out over every active user."
    )
    p_focus_arm.set_defaults(func=_cmd_focus)

    p_encourage = sub.add_parser(
        "encourage", help="Print today's recovery message if the day's gone rough."
    )
    p_encourage.add_argument("--db-path", default=None, help="Override the database path.")
    p_encourage.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_encourage.add_argument(
        "--llm", action="store_true", help="Rewrite as prose via Ollama (falls back)."
    )
    p_encourage.add_argument(
        "-o", "--output", default=None, help="Write to a file instead of stdout."
    )
    p_encourage.set_defaults(func=_cmd_encourage)

    p_self_care = sub.add_parser(
        "self-care", help="Self-care tools (end-of-day gap review)."
    )
    self_care_sub = p_self_care.add_subparsers(dest="self_care_action", required=True)
    p_sc_review = self_care_sub.add_parser(
        "review", help="Print today's end-of-day self-care gap review."
    )
    p_sc_review.add_argument("--db-path", default=None, help="Override the database path.")
    p_sc_review.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_sc_review.set_defaults(func=_cmd_self_care)

    p_open_day = sub.add_parser(
        "open-day",
        help="Answer the brief's open-day choice (relax/accomplish/ask/status).",
    )
    p_open_day.add_argument("--db-path", default=None, help="Override the database path.")
    p_open_day.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_open_day.add_argument(
        "open_day_choice",
        choices=["relax", "accomplish", "ask", "status"],
        help="relax = rest days · accomplish = light plan · ask = clear · status.",
    )
    p_open_day.set_defaults(func=_cmd_open_day)

    p_coach = sub.add_parser(
        "coach", help="Run one coaching tick: what's due, on which channel."
    )
    p_coach.add_argument("--db-path", default=None, help="Override the database path.")
    p_coach.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_coach.add_argument(
        "--all-users",
        action="store_true",
        help="Fan the tick over every active user (one job for the whole household).",
    )
    p_coach.add_argument(
        "--dry-run",
        action="store_true",
        help="Show cues before suppression/channel choice; record nothing.",
    )
    p_coach.add_argument(
        "--deliver",
        action="store_true",
        help="Actually publish each fired decision via APNs/Twilio/TTS (else just print).",
    )
    p_coach.set_defaults(func=_cmd_coach)

    p_usage = sub.add_parser(
        "usage",
        help="Feature-usage loop: list usage, mute/un-mute a module, or run the weekly nudge.",
    )
    p_usage.add_argument("--db-path", default=None, help="Override the database path.")
    p_usage.add_argument("--user", default=None, help="Handle of the user to act on.")
    usage_sub = p_usage.add_subparsers(dest="usage_action", required=True)
    usage_sub.add_parser("list", help="Show usage buckets (using/ignored/dormant) + muted.")
    u_mute = usage_sub.add_parser("mute", help="Mute a module's nudges for this user.")
    u_mute.add_argument("feature", help="Module key to mute (e.g. location_anchor).")
    u_unmute = usage_sub.add_parser("unmute", help="Turn a muted module back on.")
    u_unmute.add_argument("feature", help="Module key to un-mute.")
    u_check = usage_sub.add_parser(
        "check", help="Run the weekly usage nudge now (once/week; --deliver to send)."
    )
    u_check.add_argument(
        "--deliver",
        action="store_true",
        help="Actually send the push (else a dry run that only reports).",
    )
    u_check.add_argument(
        "--all-users",
        action="store_true",
        help="Fan the weekly check over every active user (one job for the box).",
    )
    p_usage.set_defaults(func=_cmd_usage)

    p_panic = sub.add_parser(
        "panic", help="Overwhelmed? Triage what's on fire now + one first step."
    )
    p_panic.add_argument("--db-path", default=None, help="Override the database path.")
    p_panic.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_panic.add_argument(
        "--llm", action="store_true", help="Rewrite as prose via Ollama (falls back)."
    )
    p_panic.add_argument("-o", "--output", default=None, help="Write to a file instead of stdout.")
    p_panic.set_defaults(func=_cmd_panic)

    p_next = sub.add_parser(
        "next", help="The single honest next thing to do right now (never the list)."
    )
    p_next.add_argument("--db-path", default=None, help="Override the database path.")
    p_next.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_next.add_argument("-o", "--output", default=None, help="Write to a file instead of stdout.")
    p_next.set_defaults(func=_cmd_next)

    p_day = sub.add_parser(
        "day", help="Today's visual day-shape — the day as a timeline of blocks."
    )
    p_day.add_argument("--db-path", default=None, help="Override the database path.")
    p_day.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_day.add_argument("-o", "--output", default=None, help="Write to a file instead of stdout.")
    p_day.set_defaults(func=_cmd_day)

    p_notify = sub.add_parser(
        "notify",
        help="Send a test notification through the configured route (native APNs push).",
    )
    p_notify.add_argument(
        "--test",
        action="store_true",
        help="Send a test notification (the default and only action today).",
    )
    p_notify.add_argument(
        "-m", "--message", default=None, help="Custom message text (default: a canned test)."
    )
    p_notify.add_argument(
        "--channel",
        choices=["push", "sound", "voice"],
        default="push",
        help="Delivery channel → priority (push=normal, sound=high, voice=max).",
    )
    p_notify.add_argument("--db-path", default=None, help="Override the database path.")
    p_notify.add_argument("--user", default=None, help="Handle of the user to notify.")
    p_notify.set_defaults(func=_cmd_notify)

    p_vacation = sub.add_parser(
        "vacation", help="Ease off the nudges while away (on/off/status)."
    )
    p_vacation.add_argument(
        "action", nargs="?", choices=["on", "off", "status"], default="status",
        help="Turn vacation mode on, off, or print its state (default: status).",
    )
    p_vacation.add_argument("--db-path", default=None, help="Override the database path.")
    p_vacation.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_vacation.set_defaults(func=_cmd_vacation)


def _cmd_vacation(args: argparse.Namespace) -> int:
    """Turn vacation mode on/off, or print its state (``on`` / ``off`` / ``status``).

    Vacation mode eases off the nudges for a multi-day, away-from-home stretch: the
    coaching tick then holds every discretionary cue (as quiet hours does),
    leaving time-critical calendar obligations and on-demand surfaces untouched.
    This is the manual control (the location-cued auto-resume on returning home is
    wired into the trip state machine); ``status`` is the default and read-only.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``action``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.vacation import activate, deactivate, vacation_status

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    action = args.action or "status"
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        if action == "on":
            status = activate(store, now=utcnow(), source="manual")
        elif action == "off":
            status = deactivate(store)
        else:
            status = vacation_status(store)

    if status["active"]:
        since = status.get("since")
        src = status.get("source")
        detail = f" (since {since})" if since else ""
        detail += f" [{src}]" if src else ""
        print(f"🏝️  Vacation mode is ON{detail} — non-urgent nudges are eased off.")
    else:
        print("Vacation mode is OFF — nudges run as normal.")
    return 0


def _cmd_usage(args: argparse.Namespace) -> int:
    """Feature-usage loop: list usage, mute/un-mute a module, or run the weekly nudge.

    - ``list`` — the /stats buckets (leaning on / firing but ignored / dormant),
      plus which modules are muted.
    - ``mute <feature>`` / ``unmute <feature>`` — silence a module's nudges (or
      turn them back on) for this user.
    - ``check`` — run the weekly usage nudge now (``--deliver`` to actually send;
      otherwise a dry run that only reports what it would send).

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``usage_action``,
            and (for mute/unmute) ``feature``, (for check) ``deliver``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.stats import _feature_usage
    from prefrontal.usage import run_usage_check

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        action = args.usage_action

        def _report_check(store: MemoryStore, handle: str) -> None:
            report = run_usage_check(
                store, settings=settings, handle=handle, deliver=args.deliver
            )
            if not report.get("feature"):
                print(f"No nudge: {report.get('reason', 'nothing to do')}.")
                return
            if report.get("delivered"):
                verb, tail = "Delivered", " Reply Mute/Keep on the push."
            elif args.deliver:
                verb = "Tried to nudge"
                tail = " (no push went out — check your APNs route.)"
            else:
                verb, tail = "Would nudge", " (dry run — pass --deliver to send.)"
            print(
                f"{verb}: “{report['feature']}” (offered {report['offered']}×, "
                f"acted on {report['engaged']}).{tail}"
            )

        if action == "check":
            # --all-users fans the weekly nudge over every active user (one job for
            # the box); each run self-gates to once per ISO week per user.
            if getattr(args, "all_users", False):
                for handle, store in _user_targets(unscoped, args):
                    print(f"== {handle} ==")
                    _report_check(store, handle)
            else:
                store = _resolve_user_store(unscoped, args.user)
                handle = next(
                    (u["handle"] for u in unscoped.list_users() if u["id"] == store.user_id),
                    "",
                )
                _report_check(store, handle)
            return 0

        # mute / unmute / list act on a single user.
        store = _resolve_user_store(unscoped, args.user)

        if action in ("mute", "unmute"):
            store.set_feature_muted(args.feature, action == "mute")
            verb = "Muted" if action == "mute" else "Un-muted"
            now_muted = ", ".join(sorted(store.muted_features())) or "(none)"
            print(f"{verb} {args.feature}. Now muted: {now_muted}")
            return 0

        # list
        fu = _feature_usage(store)
        s = fu["summary"]
        print(
            f"Feature usage (last {fu['window_days']}d): "
            f"{s['using']} in use · {s['ignored']} ignored · "
            f"{s['dormant']} dormant · {s['muted']} muted"
        )
        for f in fu["features"]:
            if not (f["offered"] or f["engaged"] or f["invoked"] or f["muted"]):
                continue
            tag = {"using": "✓", "ignored": "⚠", "dormant": "·"}[f["bucket"]]
            mute = " 🔕" if f["muted"] else ""
            bits = []
            if f["offered"]:
                r = f["engagement_rate"]
                rate = "" if r is None else f" ({round(r * 100)}% acted on)"
                bits.append(f"offered {f['offered']}{rate}")
            if f["invoked"]:
                bits.append(f"opened {f['invoked']}×")
            print(f"  {tag} {f['feature']:<18} {', '.join(bits)}{mute}")
        return 0


def _cmd_focus(args: argparse.Namespace) -> int:
    """Arm a focus session from a live calendar block (native focus-arm tick).

    The launchd-native twin of ``POST /webhooks/focus/arm``: when a
    "focus"/"deep work" event is happening now and no session is running, it
    auto-starts one for the rest of that window (the coaching tick then delivers
    that session's interrupts). Idempotent — safe on a short interval — and it
    shares :func:`prefrontal.modules.hyperfocus.arm_focus_session` with the
    endpoint so the two can't drift. ``--all-users`` fans over every active user
    (one job for the box); ``deploy/coach.sh`` runs it on the coaching tick.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``all_users``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.modules.hyperfocus import arm_focus_session

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        for handle, store in _user_targets(unscoped, args):
            if getattr(args, "all_users", False):
                print(f"== {handle} ==")
            result = arm_focus_session(store, settings)
            if result.get("armed"):
                mins = result.get("planned_minutes")
                tail = f" ({mins:g} min left)" if isinstance(mins, (int, float)) else ""
                print(f"Armed focus: “{result['intended_task']}”{tail}.")
            else:
                print(f"No arm: {result.get('reason', 'nothing to do')}.")
    return 0


def _briefing_text(store: MemoryStore, settings, *, llm: bool) -> str:
    """Render today's briefing for a scoped user — deterministic, or LLM prose.

    Shared by the print/write path and the ``--deliver`` push so both send the
    same text. On ``--llm`` a down Ollama falls back to the structured briefing
    (a note goes to stderr), so it always produces something.
    """
    if llm:
        from prefrontal.integrations import ProviderResolver

        # Claude when the ``briefing`` agent is opted into Anthropic, else local.
        client = ProviderResolver.from_settings(settings).client("briefing")
        result = summarize_briefing(store, client=client)
        if result.source == "heuristic":
            print(
                "Ollama unavailable; using the structured briefing.",
                file=sys.stderr,
            )
        return result.text
    return render_briefing(build_briefing(store))


def _cmd_briefing(args: argparse.Namespace) -> int:
    """Print today's morning briefing (deterministic, or LLM prose with --llm).

    With ``--deliver`` it publishes the digest as a push through the user's own
    delivery route (the native twin of the ``morning-briefing`` n8n workflow),
    honoring ``--all-users`` so one launchd job briefs the whole box; otherwise
    it prints (or writes ``--output``) for a single user.

    Args:
        args: Parsed arguments; uses ``db_path``, ``llm``, ``output``, ``user``,
            ``deliver``, ``all_users``, ``channel``.

    Returns:
        Process exit code (0 on success; 1 if ``--deliver`` sent nothing).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        if getattr(args, "deliver", False):
            return _deliver_briefing(unscoped, args, settings)
        store = _resolve_user_store(unscoped, args.user)
        text = _briefing_text(store, settings, llm=args.llm)
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote briefing to {args.output}")
    else:
        print(text, end="")
    return 0


def _deliver_briefing(unscoped: MemoryStore, args: argparse.Namespace, settings) -> int:
    """Publish the briefing as a push per user; return 0 if any send succeeded.

    A plain notification (no one-tap buttons) on the ``push`` channel — the
    briefing is the digest, so unlike an ``ambient`` cue it actually sends. Uses
    the same per-user :func:`~prefrontal.delivery.resolve_route` +
    :class:`~prefrontal.delivery.DeliveryClient` the coaching tick
    uses, so a user with no route of their own is skipped, never delivered to the
    operator's device (no cross-account leak on a multi-user box).
    """
    from prefrontal.coaching import Cue, Decision
    from prefrontal.delivery import DeliveryClient, resolve_route

    client = DeliveryClient.from_settings(settings)
    any_sent = False
    for handle, store in _user_targets(unscoped, args):
        if getattr(args, "all_users", False):
            print(f"== {handle} ==")
        text = _briefing_text(store, settings, llm=args.llm)
        route = resolve_route(store, settings)
        cue = Cue(
            module="briefing",
            intervention="morning",
            urgency="nudge",
            text=text,
            context_key="briefing",  # unmapped → a plain push, no action buttons
            dedup_key="morning_briefing",
        )
        decision = Decision(cue=cue, channel=args.channel, text=text)
        result = client.deliver(
            decision,
            route,
            base_url=settings.oauth_base_url,
            secret=settings.session_secret,
            handle=handle,
        )
        any_sent = any_sent or result.delivered
        status = "sent" if result.delivered else "not sent"
        print(f"  → {result.transport}: {status} ({result.detail})")
    return 0 if any_sent else 1


def _cmd_self_care(args: argparse.Namespace) -> int:
    """Print today's end-of-day self-care gap review (read-only).

    Reads today's self-care confirms back as a timeline and surfaces the gaps a
    raw tally hides — a late first glass of water, a long stretch between bio
    breaks, a quota that finished short — alongside what went well. Safe to run
    any time against the live DB; it never writes.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.self_care_review import render_review, self_care_review

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        review = self_care_review(store, utcnow(), settings.timezone)
    print(render_review(review))
    return 0


def _cmd_encourage(args: argparse.Namespace) -> int:
    """Print today's encouragement/recovery message when the day's gone rough.

    Deterministic by default (``--llm`` for warmer Ollama prose, falling back).
    Prints a plain note when the day isn't rough (or the layer is off), so it's
    safe to run against the live DB for testing the trigger without n8n.

    Args:
        args: Parsed arguments; uses ``db_path``, ``llm``, ``output``, ``user``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        if args.llm:
            result = summarize_encouragement(store)
            text = result.text
            if not result.rough:
                print("Today isn't reading as rough — nothing to send.")
                return 0
            if result.source == "heuristic":
                print(
                    "Ollama unavailable; printing the deterministic message.",
                    file=sys.stderr,
                )
        else:
            now = utcnow()
            assessment = assess_day(store, now=now)
            if not assessment.rough:
                reason = "layer is off" if not assessment.enabled else "not a rough day"
                print(f"Today isn't reading as rough ({reason}) — nothing to send.")
                return 0
            text = render_encouragement(
                assessment, build_recovery(store, assessment, now=now)
            )
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote encouragement to {args.output}")
    else:
        print(text, end="")
    return 0


def _cmd_open_day(args: argparse.Namespace) -> int:
    """Set the standing answer to the morning brief's open-day choice (§6.2).

    On a wide-open day the briefing asks whether you'd rather rest or make it
    count; this records that answer so future open days act on it. ``relax`` →
    permission to rest (plus one optional low-stakes item); ``accomplish`` → a
    light plan built from the day's free windows; ``ask`` clears it so the brief
    asks again. ``status`` prints the current setting.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``open_day_choice``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.encouragement import OPEN_DAY_CHOICES, OPEN_DAY_KEY

    settings = get_settings()
    with MemoryStore.open(args.db_path or settings.db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        choice = args.open_day_choice
        if choice == "status":
            current = store.get_state(OPEN_DAY_KEY) or "(unset — the brief will ask)"
            print(f"Open-day choice: {current}")
        elif choice in OPEN_DAY_CHOICES:
            store.set_state(OPEN_DAY_KEY, choice, source="explicit")
            print(f"Open days set to '{choice}'.")
        else:  # "ask"
            store.set_state(OPEN_DAY_KEY, "", source="explicit")
            print("Open-day choice cleared — the brief will ask next open day.")
    return 0


def _cmd_coach(args: argparse.Namespace) -> int:
    """Run one coaching tick and print the decisions (or cues with --dry-run).

    Asks every enabled module "anything due right now?" and applies channel
    choice + suppression (quiet hours + debounce). ``--dry-run`` shows the raw
    cues *before* suppression/channel choice and never records a fire, so it's
    safe to run repeatedly while debugging. Without it, fired decisions are
    stamped for debounce so the same cue won't repeat next tick.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``dry_run``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        # --all-users fans the tick over every active user (one launchd job for
        # the whole household); otherwise the single --user (or sole user).
        for handle, store in _user_targets(unscoped, args):
            if args.all_users:
                print(f"== {handle} ==")
            _coach_tick(unscoped, store, args, settings)
    return 0


def _coach_tick(
    unscoped: MemoryStore,
    store: MemoryStore,
    args: argparse.Namespace,
    settings,
) -> None:
    """Run one coaching tick for a single (scoped) user.

    Collects due cues, applies channel choice + suppression, records fires, and
    (with ``--deliver``) publishes them plus the proactive overwhelm nudge.
    ``--dry-run`` prints the raw cues and returns without mutating state.
    """
    from prefrontal.coaching import run_coaching_tick
    from prefrontal.integrations.ollama import OllamaClient

    now = utcnow()
    if args.dry_run:
        # Read-only debug path: show the raw cues without running any of the
        # (mutating) sweeps or recording anything.
        ctx = build_context(store, now=now, timezone=settings.timezone)
        cues = collect_cues(store, enabled_modules(settings), ctx)
        # The encouragement/recovery layer is a non-module cue producer folded into
        # the real tick (spec §9); show it here too so --dry-run matches what fires.
        cues.extend(encouragement_cues(store, ctx))
        if not cues:
            print("No cues due.")
        for c in cues:
            print(f"[{c.urgency}] {c.module}/{c.intervention}: {c.text}")
        return
    # The whole tick — the sweeps (in order, before cue collection), collect,
    # decide, and record — is the shared run_coaching_tick service, so this CLI
    # and POST /webhooks/coach/check can't drift on what a tick does.
    tick_ollama = OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)
    result = run_coaching_tick(store, settings=settings, ollama=tick_ollama, now=now)
    if result.swept_stale:
        print(f"({result.swept_stale} unanswered nudge(s) logged as channel misses)")
    if result.decomposed:
        print(f"({result.decomposed} avoided task(s) broken down)")
    if result.clarified:
        print(f"({result.clarified} ambiguous item(s) flagged for clarification)")
    decisions = result.decisions
    for d in decisions:
        print(f"[{d.channel}] {d.cue.module}/{d.cue.intervention}: {d.text}")
    if not decisions:
        print(f"Nothing to say right now ({len(result.cues)} cue(s) held).")
    if decisions and args.deliver:
        _deliver_decisions(unscoped, store, decisions, settings)
    # Proactive overwhelm nudge — its own edge/cooldown/quiet-hours-defer
    # decision, delivered on this same native tick so panic no longer needs an
    # n8n poll of /webhooks/panic/check. Only on a delivering tick, so a
    # print-only run doesn't consume the overwhelm edge.
    if args.deliver:
        _deliver_panic(unscoped, store, settings, now)


def _deliver_decisions(unscoped, store, decisions, settings) -> None:
    """Publish fired decisions via the native delivery client and print outcomes.

    Resolves the acting user's routing (per-user ``coaching_state`` over operator
    defaults) and delivers each decision on its chosen channel. Signs the one-tap
    action buttons with the user's handle so a background tap authenticates.
    """
    from prefrontal.delivery import DeliveryClient, resolve_route

    handle = next(
        (u["handle"] for u in unscoped.list_users() if u["id"] == store.user_id), ""
    )
    route = resolve_route(store, settings)
    client = DeliveryClient.from_settings(settings)
    results = client.deliver_all(
        decisions,
        route,
        base_url=settings.oauth_base_url,
        secret=settings.session_secret,
        handle=handle,
    )
    for result in results:
        status = "sent" if result.delivered else "not sent"
        print(f"  → {result.transport}: {status} ({result.detail})")


def _deliver_panic(unscoped, store, settings, now) -> None:
    """Run the proactive-panic decision and, if it fires, deliver it natively.

    Uses the same :func:`~prefrontal.panic.evaluate_panic_check` the
    ``/webhooks/panic/check`` endpoint does (edge-trigger + quiet-hours defer +
    cooldown + pending-step record), then publishes the overwhelm nudge — with the
    "Open triage" and, when ack-able, signed "✓ Did it" buttons — through the
    native delivery client. So a scheduled ``coach --deliver`` covers panic without
    an n8n poll. A no-op when nothing tips into overwhelm.
    """
    from prefrontal.coaching import Cue, Decision, in_quiet_hours
    from prefrontal.delivery import DeliveryClient, resolve_route
    from prefrontal.panic import evaluate_panic_check
    from prefrontal.webhooks.notify import nudge_actions, panic_actions

    ackable = bool(settings.oauth_base_url and settings.session_secret)
    result = evaluate_panic_check(
        store,
        now=now,
        quiet_hours=in_quiet_hours(store, now, settings.timezone),
        ackable=ackable,
    )
    if not result.fire:
        return
    handle = next(
        (u["handle"] for u in unscoped.list_users() if u["id"] == store.user_id), ""
    )
    actions = panic_actions(settings.oauth_base_url)
    if result.step_id is not None:
        actions = (
            nudge_actions(
                "panic", result.step_id,
                base_url=settings.oauth_base_url, secret=settings.session_secret, handle=handle,
            )
            + actions
        )
    # "sound" (high priority), not "voice": panic must reach the phone with its
    # buttons, never divert to local TTS on a mini with tts_enabled.
    cue = Cue(
        module="panic", intervention="proactive_alert", urgency="critical",
        text=result.message, context_key="panic", dedup_key="panic_alert",
    )
    decision = Decision(cue=cue, channel="sound", text=result.message)
    route = resolve_route(store, settings)
    client = DeliveryClient.from_settings(settings)
    res = client.deliver(
        decision, route,
        base_url=settings.oauth_base_url, secret=settings.session_secret, handle=handle,
        extra_actions=actions,
    )
    print(f"  → panic: {'sent' if res.delivered else 'not sent'} ({res.detail})")


def _cmd_notify(args: argparse.Namespace) -> int:
    """Send a test notification through the user's configured delivery route.

    Exercises the real delivery stack end-to-end — the same
    :class:`~prefrontal.delivery.DeliveryClient` and per-user
    :func:`~prefrontal.delivery.resolve_route` the coaching tick uses
    — so it confirms native APNs push is wired up (device token + ``APNS_*`` signing
    creds) before you rely on a nudge landing. Prints where it routed and the
    transport's result. It sends a plain push (no action buttons), so the
    *button*-signing config (``OAUTH_BASE_URL``/``SESSION_SECRET``) isn't needed —
    but the APNs creds above still are.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``message``, ``channel``.

    Returns:
        ``0`` if a send succeeded, ``1`` if nothing was delivered (e.g. no
        transport configured, or the transport returned an error).
    """
    from prefrontal.coaching import Cue, Decision
    from prefrontal.delivery import DeliveryClient, resolve_route

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    message = args.message or "✅ Prefrontal test notification — your delivery route works."
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        handle = next(
            (u["handle"] for u in unscoped.list_users() if u["id"] == store.user_id), ""
        )
        route = resolve_route(store, settings)
        cue = Cue(
            module="notify",
            intervention="test",
            urgency="nudge",
            text=message,
            context_key="test",  # unmapped → a plain push, no action buttons
            dedup_key="notify_test",
        )
        decision = Decision(cue=cue, channel=args.channel, text=message)
        client = DeliveryClient.from_settings(settings)
        result = client.deliver(
            decision,
            route,
            base_url=settings.oauth_base_url,
            secret=settings.session_secret,
            handle=handle,
        )

    if route.apns_token and client.apns.configured:
        dest = "apns → device token (native push)"
    elif settings.ntfy_dev and route.ntfy_topic:
        dest = f"ntfy [dev shim] → {route.ntfy_server}/{route.ntfy_topic}"
    else:
        dest = "(no transport configured)"
    print(f"route:   {dest}")
    print(
        f"result:  channel={decision.channel} transport={result.transport} "
        f"delivered={result.delivered} status={result.status_code}"
    )
    print(f"detail:  {result.detail}")
    if not result.delivered:
        if result.transport == "none":
            print(
                "\nNothing was sent — no transport is configured for this user. "
                "Register the device's APNs token (the app does this on first launch, "
                "or `prefrontal user route <handle> --apns-token …`) and set the "
                "APNS_* signing creds in the environment. For a free-signing dev "
                "build, enable the ntfy shim with PREFRONTAL_NTFY_DEV=1.",
                file=sys.stderr,
            )
        return 1
    return 0


def _cmd_panic(args: argparse.Namespace) -> int:
    """Print the panic-mode triage: what's on fire and one first step.

    Unlike the morning briefing (a calm whole-day overview), this ranks only what
    is bearing down *right now* across calendar, todos, and mail, and hands back a
    single concrete first action to break the freeze.

    Args:
        args: Parsed arguments; uses ``db_path``, ``llm``, ``output``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        if args.llm:
            result = summarize_panic(store)
            text = result.text
            if result.source == "heuristic":
                print(
                    "Ollama unavailable; printing the structured panic triage.",
                    file=sys.stderr,
                )
        else:
            text = render_panic(build_panic(store))
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote panic triage to {args.output}")
    else:
        print(text, end="")
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    """Print the single honest next thing to do right now.

    The quiet, always-on sibling of ``panic``: where panic names every fire, this
    surfaces exactly one action — the mid-flight task you're in, a commitment to
    leave for, the worst clock-bound fire, or the avoided-but-important todo — and
    withholds the rest. See :mod:`prefrontal.next_thing`.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``output``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.next_thing import build_next_thing, render_next_thing

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        text = render_next_thing(build_next_thing(store))
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote the next thing to {args.output}")
    else:
        print(text, end="")
    return 0


def _cmd_day(args: argparse.Namespace) -> int:
    """Print today's visual day-shape — the day as a timeline of blocks.

    Today's commitments as fixed anchors, the open todos fitted into the forward
    gaps, and the free time in between (the Structured / Tiimo pattern), rendered
    as a monochrome vertical timeline so it reads as a *shape* even in a plain
    terminal. See :mod:`prefrontal.day_shape`.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``output``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.day_shape import build_day_shape, render_day_shape

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        text = render_day_shape(build_day_shape(store))
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote the day-shape to {args.output}")
    else:
        print(text, end="")
    return 0


