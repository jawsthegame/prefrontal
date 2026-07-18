"""``prefrontal learn`` / ``profile`` / ``balance`` / ``body-double`` / ``summarize``.

Derived-insight commands: recompute learned patterns, print/write the behavioral
profile (or one todo's behavior model), the work/life focus balance, the
body-double stall report, and the LLM profile summary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prefrontal.cli._common import _resolve_user_store, _user_targets
from prefrontal.config import get_settings
from prefrontal.impact import utcnow
from prefrontal.memory.patterns import recompute_patterns
from prefrontal.memory.store import MemoryStore
from prefrontal.memory.summarizer import build_profile, cache_summary, summarize_profile
from prefrontal.modules.hyperfocus import adapt_soft_block
from prefrontal.modules.self_care import adapt_self_care
from prefrontal.modules.time_blindness import adapt_morning_routine


def register(sub) -> None:
    """Attach the learn/profile/balance/body-double/summarize subcommands."""
    p_learn = sub.add_parser(
        "learn", help="Recompute derived patterns from accumulated episodes."
    )
    p_learn.add_argument("--db-path", default=None, help="Override the database path.")
    p_learn.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_learn.add_argument(
        "--all-users", action="store_true", help="Fan out over every active user."
    )
    p_learn.set_defaults(func=_cmd_learn)

    p_profile = sub.add_parser("profile", help="Print the current behavioral profile.")
    p_profile.add_argument("--db-path", default=None, help="Override the database path.")
    p_profile.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_profile.add_argument(
        "-o", "--output", default=None, help="Write to a file instead of stdout."
    )
    p_profile.add_argument(
        "--todo",
        type=int,
        default=None,
        metavar="ID",
        help="Print the queryable behavioral model for one todo instead of the "
        "whole-profile snapshot (its reschedule/snooze history + agent context).",
    )
    p_profile.set_defaults(func=_cmd_profile)

    p_balance = sub.add_parser(
        "balance", help="Show out-of-home time by life-sphere (shop/work/home/kids/personal)."
    )
    p_balance.add_argument("--db-path", default=None, help="Override the database path.")
    p_balance.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_balance.add_argument(
        "--days", type=int, default=7, help="Look-back window in days (default 7)."
    )
    p_balance.set_defaults(func=_cmd_balance)

    p_bd = sub.add_parser(
        "body-double", help="Start a timed start-together sprint on a stalled todo."
    )
    p_bd.add_argument("--db-path", default=None, help="Override the database path.")
    p_bd.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_bd.add_argument(
        "--todo", type=int, default=None, help="Todo id to start on (default: worst-avoided)."
    )
    p_bd.add_argument(
        "--minutes", type=int, default=None,
        help="Sprint length (default: body_double_window_minutes).",
    )
    p_bd.set_defaults(func=_cmd_body_double)

    p_summarize = sub.add_parser(
        "summarize", help="LLM-summarize the profile (Ollama) to profile-<handle>.md."
    )
    p_summarize.add_argument("--db-path", default=None, help="Override the database path.")
    p_summarize.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_summarize.add_argument(
        "--all-users", action="store_true", help="Fan out over every active user."
    )
    p_summarize.add_argument(
        "-o", "--output", default=None, help="Output path (default: profile-<handle>.md)."
    )
    p_summarize.add_argument("--model", default=None, help="Override the Ollama model.")
    p_summarize.add_argument(
        "--no-fallback",
        action="store_true",
        help="Fail instead of falling back to the structured profile.",
    )
    p_summarize.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip writing the narrative to the profile cache (file only).",
    )
    p_summarize.set_defaults(func=_cmd_summarize)


def _cmd_learn(args: argparse.Namespace) -> int:
    """Recompute derived patterns from accumulated episodes.

    Args:
        args: Parsed arguments; uses ``args.db_path``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as store:
        for label, s in _user_targets(store, args):
            summary = recompute_patterns(s, timezone=settings.timezone)
            by_type = (
                ", ".join(f"{n} {t}" for t, n in sorted(summary.by_type.items()))
                or "none"
            )
            windowed = (
                f" (window {summary.window_days:g}d dropped {summary.windowed_out})"
                if summary.window_days
                else ""
            )
            print(f"[{label}] recomputed patterns from {summary.episodes} episodes{windowed}.")
            print(f"[{label}] patterns written: {summary.patterns} ({by_type})")
            if summary.bias is not None:
                print(f"[{label}] time_estimation_bias -> {summary.bias}")
            if summary.band_bias:
                bands = ", ".join(f"{b}={v}" for b, v in sorted(summary.band_bias.items()))
                print(f"[{label}] time-of-day bias -> {bands}")
            if summary.auto_half_lives:
                hls = ", ".join(f"{b}={v:g}d" for b, v in sorted(summary.auto_half_lives.items()))
                print(f"[{label}] auto half-life -> {hls}")
            if summary.early_start_threshold is not None:
                print(f"[{label}] early_start_threshold -> {summary.early_start_threshold}")
            if summary.travel_pad_fraction is not None:
                print(
                    f"[{label}] travel_pad_fraction -> {summary.travel_pad_fraction} "
                    f"({round(summary.travel_pad_fraction * 100)}%)"
                )
            if summary.type_bias:
                types = ", ".join(f"{t}={v}" for t, v in sorted(summary.type_bias.items()))
                print(f"[{label}] task-type bias -> {types}")
            if summary.energy_bias:
                energies = ", ".join(f"{e}={v}" for e, v in sorted(summary.energy_bias.items()))
                print(f"[{label}] energy bias -> {energies}")
            if summary.category_bias:
                cats = ", ".join(f"{c}={v}" for c, v in sorted(summary.category_bias.items()))
                print(f"[{label}] category bias -> {cats}")
            if summary.activity_bias:
                acts = ", ".join(f"{a}={v}" for a, v in sorted(summary.activity_bias.items()))
                print(f"[{label}] activity bias -> {acts}")
            cal = summary.calibration
            if cal is not None and cal.status == "ok":
                if cal.helps:
                    verdict = "helping"
                elif summary.bias_pre_decay is not None:
                    verdict = (
                        f"NOT helping — auto-decayed "
                        f"{summary.bias_pre_decay} -> {summary.bias}"
                    )
                else:
                    verdict = "NOT helping — consider a reset"
                print(
                    f"[{label}] bias check: error {cal.raw_error} -> {cal.adjusted_error} "
                    f"on {cal.samples} recent ({verdict})"
                )
            ccal = summary.channel_calibration
            if ccal is not None and ccal.status == "ok":
                if ccal.helps:
                    cverdict = "helping"
                elif summary.channel_decayed:
                    cverdict = "NOT helping — auto-damped channel rates toward pooled"
                else:
                    cverdict = "NOT helping — channel signal is noise"
                print(
                    f"[{label}] channel check: error {ccal.baseline_error} -> "
                    f"{ccal.adjusted_error} on {ccal.samples} recent ({cverdict})"
                )
            # Learned receptivity model (M3): does conditioning on context predict
            # acknowledgement better than pooled? The honesty gate — the learned
            # gate only supersedes the rules gate once this says "helping".
            rcal = summary.receptivity_calibration
            if rcal is not None and rcal.status == "ok":
                rverdict = (
                    "helping — learned gate active"
                    if rcal.helps
                    else "NOT helping — rules gate retained"
                )
                print(
                    f"[{label}] receptivity check: error {rcal.baseline_error} -> "
                    f"{rcal.adjusted_error} on {rcal.samples} recent ({rverdict})"
                )
            # Sensor precision (learning §2 feedback): are the LLM sensor's
            # proposals worth keeping? Persists the verdict + flags chronically
            # rejected targets, which the extraction prompt then de-emphasizes.
            from prefrontal.sensor import (
                recompute_proposal_durability,
                recompute_sensor_calibration,
            )

            sc = recompute_sensor_calibration(s)
            if sc.status == "ok":
                flagged = (
                    f"; chronically rejected: {', '.join(sc.flagged)}" if sc.flagged else ""
                )
                print(
                    f"[{label}] sensor precision: {sc.accepted}/{sc.resolved} accepted "
                    f"({sc.accept_rate}){flagged}"
                )
            # Post-acceptance outcome (learning §2): did accepted settings stick, or
            # get reversed? A diagnostic complement to precision above.
            dur = recompute_proposal_durability(s)
            if dur.status == "ok":
                rev = (
                    f"; reversed: {', '.join(dur.reversed_targets)}"
                    if dur.reversed_targets
                    else ""
                )
                print(
                    f"[{label}] sensor durability: {dur.held_up}/{dur.evaluated} settings "
                    f"still standing ({dur.durability_rate}){rev}"
                )
            for c in adapt_self_care(s):
                arrow = "->" if c["changed"] else "="
                print(
                    f"[{label}] self-care {c['check']} interval {arrow} "
                    f"{c['interval']}m ({c['reason']})"
                )
            routine = adapt_morning_routine(s)
            if routine["changed"] or routine["samples"]:
                arrow = "->" if routine["changed"] else "="
                print(
                    f"[{label}] morning routine lead {arrow} {routine['routine']}m "
                    f"({routine['reason']})"
                )
            soft = adapt_soft_block(s)
            if soft["changed"] or soft["samples"]:
                arrow = "->" if soft["changed"] else "="
                print(
                    f"[{label}] hyperfocus soft block {arrow} {soft['soft_block']}m "
                    f"({soft['reason']})"
                )
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    """Build the behavioral profile and print it (or write it to a file).

    With ``--todo <id>`` it prints the *queryable* behavioral model for a single
    todo instead — the entity-scoped continuity ("you've rescheduled this four
    times") an agent retrieves on demand — rather than the whole-profile snapshot.

    Args:
        args: Parsed arguments; uses ``args.db_path``, ``args.user``, optional
            ``args.output`` and ``args.todo``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    # Don't re-seed here; just read whatever exists. initialize=True is still
    # safe and idempotent, and guarantees the tables exist for a fresh checkout.
    with MemoryStore.open(db_path) as store:
        scoped = _resolve_user_store(store, args.user)
        if args.todo is not None:
            return _print_todo_behavior(scoped, args.todo)
        profile = build_profile(scoped)
    if args.output:
        Path(args.output).write_text(profile)
        print(f"Wrote profile to {args.output}")
    else:
        print(profile, end="")
    return 0


def _print_todo_behavior(store: MemoryStore, todo_id: int) -> int:
    """Print the behavioral model for one todo (the ``profile --todo`` path)."""
    from prefrontal.memory.behavioral import todo_behavior

    behavior = todo_behavior(store, todo_id)
    if behavior is None:
        print(f"No todo {todo_id}.")
        return 1
    print(f"Todo {behavior.todo_id}: {behavior.title} [{behavior.status}]")
    print(f"  rescheduled: {behavior.reschedule_count}", end="")
    if behavior.last_rescheduled_ago:
        print(f" (last {behavior.last_rescheduled_ago})", end="")
    print()
    print(f"  snoozed: {behavior.defer_count}", end="")
    if behavior.currently_snoozed:
        print(" (currently parked)", end="")
    print()
    if behavior.days_open is not None:
        print(f"  open: {round(behavior.days_open)} days")
    if behavior.estimate_bias is not None:
        print(f"  estimate bias: {behavior.estimate_bias}x")
    if behavior.context_lines:
        print("\nAgent context:")
        for line in behavior.context_lines:
            print(f"  {line}")
    else:
        print("\nAgent context: (nothing notable yet)")
    return 0


def _cmd_balance(args: argparse.Namespace) -> int:
    """Print the focus-balance rollup — out-of-home time by life-sphere.

    Reads completed closed-loop trips over the last ``--days`` and buckets their
    time-out into shop/work/home/kids/personal (plus any free-text domain in use),
    against the per-domain weekly targets in coaching state. A read-only view of
    "am I spreading my focus the way I mean to?".

    Args:
        args: Parsed arguments; uses ``args.db_path``, ``args.user``, ``args.days``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.focus_balance import build_focus_balance, format_minutes

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    days = max(1, min(args.days, 365))
    with MemoryStore.open(db_path) as store:
        balance = build_focus_balance(_resolve_user_store(store, args.user), days=days)

    if not balance.has_data:
        print(f"No completed trips in the last {days}d to balance yet.")
        return 0

    total = format_minutes(balance.total_minutes)
    print(f"Focus balance — out-of-home time, last {days}d ({total}):")
    for d in balance.domains:
        share = f"{round(balance.share(d.domain) * 100):>3d}%"
        bar = "█" * max(0, round(balance.share(d.domain) * 20))
        target = ""
        if d.has_target:
            flag = "  ⚠ light" if d.underserved else ""
            target = f"  (aim {format_minutes(d.target_minutes or 0)}{flag})"
        print(f"  {d.domain:<12} {format_minutes(d.minutes):>6}  {share} {bar}{target}")
    return 0


def _cmd_body_double(args: argparse.Namespace) -> int:
    """Start a timed start-together (body-double) sprint on a stalled todo.

    The Task Paralysis ``body_double_nudge`` intervention made real from the
    terminal: opens a short, aligned focus session on the task's tiny first step,
    so the existing focus check/end machinery gives the end check-in. With
    ``--todo`` it targets that todo; otherwise it picks your worst-avoided open
    todo (the honest "what you keep skipping"). In a household the message invites
    a co-parent to start theirs too.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``todo``, ``minutes``.

    Returns:
        Process exit code (0 on success, 1 when there's nothing to start on).
    """
    from prefrontal.modules.task_paralysis import (
        DEFAULT_BODY_DOUBLE_WINDOW_MINUTES,
        resolve_body_double_partner,
        start_body_double,
    )
    from prefrontal.todos import avoided_todos

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as store:
        s = _resolve_user_store(store, args.user)
        if args.todo is not None:
            todo = s.get_todo(args.todo)
            if todo is None or todo["status"] != "open":
                print(f"Todo {args.todo} is not open.")
                return 1
        else:
            avoided = avoided_todos(s.open_todos(exclude_delegated=True), utcnow())
            if not avoided:
                print("Nothing open to start on — no stalled todos.")
                return 1
            todo = avoided[0]["todo"]
        window = args.minutes or int(
            s.get_float("body_double_window_minutes", DEFAULT_BODY_DOUBLE_WINDOW_MINUTES)
        )
        result = start_body_double(
            s,
            todo=todo,
            window_minutes=window,
            partner=resolve_body_double_partner(s),
            name=s.get_state("user_name", "") or "",
        )
    print(result["message"])
    print(
        f"(session {result['session_id']}, {result['planned_minutes']:g} min — "
        "tap Wrap up to end)"
    )
    return 0


def _cmd_summarize(args: argparse.Namespace) -> int:
    """LLM-summarize the profile via Ollama, caching it and writing a file.

    The narrative is stored in the ``profile_cache`` table so ``GET /profile``
    can serve it without a model round-trip; run nightly after ``prefrontal
    learn``. It is also written to ``profile.md`` (or ``--output``) for
    inspection. Falls back to the structured profile if the model is
    unavailable, unless ``--no-fallback`` is given. ``--no-cache`` skips the DB
    write (file only).

    Args:
        args: Parsed arguments; uses ``db_path``, ``output``, ``model``,
            ``no_fallback``, ``no_cache``.

    Returns:
        Process exit code (0 on success, 1 if generation failed with no fallback).
    """
    from prefrontal.integrations import ProviderError, ProviderResolver
    from prefrontal.integrations.ollama import OllamaClient

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    # Local client (honors --model); the summarizer agent uses Claude instead
    # when opted into the Anthropic provider, falling back to this one.
    local = OllamaClient(
        base_url=settings.ollama_url, model=args.model or settings.ollama_model
    )
    client = ProviderResolver.from_settings(settings, ollama=local).client(
        "summarizer", fallback=local
    )
    with MemoryStore.open(db_path) as store:
        for handle, s in _user_targets(store, args):
            try:
                result = summarize_profile(
                    s, client=client, fallback=not args.no_fallback
                )
            except ProviderError as exc:
                print(f"[{handle}] summarization failed: {exc}", file=sys.stderr)
                return 1
            if not args.no_cache:
                cache_summary(s, result)
            if result.source == "heuristic":
                print(
                    f"[{handle}] model unavailable (local fallback "
                    f"{local.base_url}, model {local.model}); cached the "
                    "structured profile instead.",
                    file=sys.stderr,
                )
            # Per-user profile artifact: ``profile-<handle>.md`` unless an explicit
            # --output is given (single-user convenience).
            output = args.output or f"profile-{handle}.md"
            Path(output).write_text(result.text)
            label = f"{result.source}" + (f" ({result.model})" if result.model else "")
            where = output if args.no_cache else f"the profile cache and {output}"
            print(f"[{handle}] wrote {label} profile to {where}")
    return 0


