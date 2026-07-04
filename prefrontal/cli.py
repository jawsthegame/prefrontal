"""Command line entry point for Prefrontal.

Exposes subcommands, wired up as the ``prefrontal`` console script in
``pyproject.toml``:

- ``prefrontal init-db`` — create the SQLite memory database.
- ``prefrontal user`` — provision users (add/list/rotate/disable).
- ``prefrontal migrate-multi-tenant`` — upgrade a single-tenant DB in place.
- ``prefrontal serve`` — run the webhook listener with uvicorn.
- ``prefrontal learn`` — recompute derived patterns from accumulated episodes.
- ``prefrontal profile`` — print (or write) the structured behavioral profile.
- ``prefrontal summarize`` — LLM-summarize the profile (Ollama); cache it for
  ``GET /profile`` and write ``profile-<handle>.md``.
- ``prefrontal briefing`` — print today's morning digest (``--llm`` for prose).
- ``prefrontal coach`` — run one coaching tick: what's due + on which channel.
- ``prefrontal encourage`` — print today's recovery message if the day's rough.
- ``prefrontal panic`` — triage what's on fire right now + one first step.
- ``prefrontal todo`` — add/list/done open todos (open loops).
- ``prefrontal fit`` — show open todos that fit N minutes of free time.
- ``prefrontal mail`` — ingest/fetch/list triaged email (list/sync/fetch).

Multi-tenant: the data commands (``learn``, ``summarize``, ``profile``,
``briefing``, ``panic``, ``todo``, ``fit``, ``mail``) act on one user, chosen with
``--user <handle>``; ``learn``/``summarize`` also take ``--all-users`` to fan
out (the nightly default). A command errors if no user is given and more than
one exists.

Run ``prefrontal --help`` or ``prefrontal <command> --help`` for details.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prefrontal import __version__
from prefrontal.briefing import build_briefing, render_briefing, summarize_briefing
from prefrontal.clock import TS_FMT
from prefrontal.clock import parse_ts as _parse_last
from prefrontal.coaching import (
    DEFAULT_ACK_WINDOW_MINUTES,
    build_context,
    collect_cues,
    decide,
    note_delivered,
    record_fired,
    sweep_stale_nudges,
)
from prefrontal.config import get_settings
from prefrontal.encouragement import (
    assess_day,
    build_recovery,
    render_encouragement,
    summarize_encouragement,
)
from prefrontal.household import build_sheet, render_sheet
from prefrontal.impact import utcnow
from prefrontal.log import configure_logging
from prefrontal.mail.imap import DEFAULT_UNSEEN_WINDOW_DAYS
from prefrontal.memory.db import init_db
from prefrontal.memory.migrate import migrate_to_multi_tenant
from prefrontal.memory.patterns import recompute_patterns
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.memory.summarizer import (
    build_profile,
    cache_summary,
    summarize_profile,
)
from prefrontal.modules import available, enabled_modules
from prefrontal.modules.self_care import (
    adapt_self_care,
    mark_self_care_prompted,
    sweep_unanswered_self_care,
)
from prefrontal.panic import build_panic, render_panic, summarize_panic
from prefrontal.scheduling import fit_todos
from prefrontal.todos import (
    heuristic_category,
    record_todo_closed,
    resolve_category,
)


def _resolve_user_store(store: MemoryStore, handle: str | None) -> MemoryStore:
    """Return ``store`` scoped to a user chosen by ``handle`` (or the only one).

    Args:
        store: An unscoped store.
        handle: The user's handle, or ``None`` to auto-pick when exactly one
            user exists.

    Returns:
        A store scoped to the resolved user.

    Raises:
        SystemExit: With a clear message if the handle is unknown, or if no
            handle was given and zero/many users exist.
    """
    users = store.list_users()
    if handle is not None:
        match = next((u for u in users if u["handle"] == handle), None)
        if match is None:
            raise SystemExit(f"No such user '{handle}'. Run `prefrontal user list`.")
        return store.scoped(match["id"])
    if not users:
        raise SystemExit(
            "No users provisioned. Run `prefrontal user add <handle>` first."
        )
    if len(users) > 1:
        handles = ", ".join(u["handle"] for u in users)
        raise SystemExit(
            f"Multiple users exist ({handles}); pass --user <handle>."
        )
    return store.scoped(users[0]["id"])


def _cmd_init_db(args: argparse.Namespace) -> int:
    """Create and seed the memory database.

    Args:
        args: Parsed arguments; uses ``args.db_path``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    conn = init_db(db_path)
    store = MemoryStore(conn)
    # Coaching-state and module defaults are seeded per user at provision time
    # (`prefrontal user add`), not here — schema.sql is purely structural now.
    users = store.list_users()
    conn.close()
    print(f"Initialized memory database at {db_path}")
    print("Tables: users, episodes, patterns, coaching_state, …")
    if users:
        print(f"Existing users: {', '.join(u['handle'] for u in users)}")
    else:
        print("No users yet — run `prefrontal user add <handle> --operator`.")
    return 0


def _cmd_user(args: argparse.Namespace) -> int:
    """Provision users: add, list, rotate a token, or disable.

    Args:
        args: Parsed arguments; ``user_action`` plus action-specific fields.

    Returns:
        Process exit code (0 on success, 1 on a not-found/usage error).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as store:
        if args.user_action == "add":
            if store.get_user(args.handle) is not None:
                print(f"User '{args.handle}' already exists.", file=sys.stderr)
                return 1
            user, token = provision_user(
                store,
                args.handle,
                display_name=args.display_name or args.handle,
                is_operator=args.operator,
            )
            role = "operator" if user["is_operator"] else "user"
            print(f"Created {role} '{user['handle']}'. Token (shown once):")
            print(f"  {token}")
        elif args.user_action == "list":
            users = store.list_users()
            if not users:
                print("No users provisioned.")
            for u in users:
                op = " [operator]" if u["is_operator"] else ""
                print(f"{u['handle']} ({u['status']}){op} — {u['display_name'] or ''}")
        elif args.user_action == "rotate":
            token = store.rotate_user_token(args.handle)
            if token is None:
                print(f"No such user '{args.handle}'.", file=sys.stderr)
                return 1
            print(f"New token for '{args.handle}' (shown once):")
            print(f"  {token}")
        elif args.user_action == "disable":
            if not store.set_user_status(args.handle, "disabled"):
                print(f"No such user '{args.handle}'.", file=sys.stderr)
                return 1
            print(f"Disabled '{args.handle}'.")
    return 0


def _cmd_household(args: argparse.Namespace) -> int:
    """Manage the shared household (operator-set membership in v1).

    ``add`` creates a household; ``join`` puts a user into one; ``show`` prints the
    rendered shared sheet for a member. Membership is operator-set (see
    docs/household-sheet.md §8) — one parent wires both users in once.

    Args:
        args: Parsed arguments; ``household_action`` plus action-specific fields.

    Returns:
        Process exit code (0 on success, 1 on a not-found/usage error).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as store:
        if args.household_action == "add":
            hid = store.create_household(args.name)
            print(f"Created household '{args.name}' (id {hid}).")
            print(f"Add members: prefrontal household join <handle> --household {hid}")
        elif args.household_action == "join":
            try:
                changed = store.set_user_household(args.handle, args.household)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if not changed:
                print(f"No such user '{args.handle}'.", file=sys.stderr)
                return 1
            print(f"Put '{args.handle}' in household {args.household}.")
        elif args.household_action == "leave":
            if not store.set_user_household(args.handle, None):
                print(f"No such user '{args.handle}'.", file=sys.stderr)
                return 1
            print(f"Removed '{args.handle}' from their household.")
        elif args.household_action == "show":
            scoped = _resolve_user_store(store, args.user)
            if scoped.household_id_or_none() is None:
                print(
                    "That user isn't in a household. Create one with "
                    "`prefrontal household add`, then `... join`.",
                    file=sys.stderr,
                )
                return 1
            print(render_sheet(build_sheet(scoped)), end="")
        elif args.household_action == "star":
            return _award_stars_cli(store, args, settings)
        elif args.household_action == "prompt-check":
            return _star_prompts_cli(store, args, settings)
        elif args.household_action == "checkin-check":
            return _checkin_cli(store, args, settings)
        elif args.household_action == "digest-check":
            return _digest_cli(store, args, settings)
        elif args.household_action == "balance":
            return _balance_cli(store, args, settings)
        elif args.household_action == "shopping":
            return _shopping_cli(store, args)
        elif args.household_action == "chore":
            return _chores_cli(store, args, settings)
        elif args.household_action == "routine":
            return _routines_cli(store, args, settings)
        elif args.household_action == "chores-check":
            return _chores_check_cli(store, args, settings)
        elif args.household_action == "invite":
            scoped = _resolve_user_store(store, args.user)
            if scoped.household_id_or_none() is None:
                print("That user isn't in a household.", file=sys.stderr)
                return 1
            inv = scoped.create_invite()
            print(f"Invite code: {inv['code']}  (expires {inv['expires_at']} UTC)")
            print("Share it, or send: <base-url>/kids?invite=" + inv["code"])
        elif args.household_action == "redeem":
            scoped = _resolve_user_store(store, args.user)
            result = scoped.redeem_invite(args.code)
            if not result["ok"]:
                print(result["error"], file=sys.stderr)
                return 1
            print(f"Joined household: {result.get('household_name')}")
    return 0


def _shopping_cli(store, args) -> int:
    """List the shared shopping list, or add/check-off/remove an item."""
    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    if args.add:
        sid = scoped.add_shopping_item(
            item=args.add, spec=args.spec, where_to_buy=args.where,
            child_id=args.child or 0, added_by=scoped.user_id,
        )
        print(f"Added #{sid}: {args.add}")
        return 0
    if args.got is not None:
        if not scoped.set_shopping_got(args.got, True, user_id=scoped.user_id):
            print(f"No item #{args.got}.", file=sys.stderr)
            return 1
        print(f"Checked off #{args.got}.")
        return 0
    if args.remove is not None:
        if not scoped.remove_shopping_item(args.remove):
            print(f"No item #{args.remove}.", file=sys.stderr)
            return 1
        print(f"Removed #{args.remove}.")
        return 0
    if args.clear_got:
        cleared = scoped.clear_got_shopping_items()
        print(f"Cleared {cleared} checked-off item{'' if cleared == 1 else 's'}.")
        return 0
    items = scoped.shopping_items()
    if not items:
        print("Shopping list is empty.")
        return 0
    for s in items:
        box = "x" if s["got"] else " "
        detail = " · ".join(p for p in (s.get("spec"), s.get("where_to_buy")) if p)
        who = f" — {s['child_name']}" if s.get("child_name") else ""
        tail = f" ({detail})" if detail else ""
        print(f"  [{box}] #{s['id']} {s['item']}{tail}{who}")
    return 0


def _chores_cli(store, args, settings) -> int:
    """List the shared chores, or add/mark-done/pause/remove one.

    The CLI face of the recurring-chore feature: ``--add`` defines a chore (with
    ``--due`` required), ``--done`` marks it complete for today, ``--remove``
    deletes it, and ``--enable``/``--disable`` pause or resume its reminders.
    """
    from prefrontal.household import (
        describe_chore_days,
        fmt_time_12h,
        normalize_chore,
        with_effective_schedule,
    )
    from prefrontal.scheduling import local_datetime

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1

    if args.add:
        owner_id = None
        if args.owner:
            owner = store.get_user(args.owner)
            if owner is None or owner.get("household_id") != scoped.household_id_or_none():
                print(f"'{args.owner}' isn't a member of this household.", file=sys.stderr)
                return 1
            owner_id = owner["id"]
        routine_id = None
        if args.routine:
            match = next(
                (r for r in scoped.routines()
                 if r["title"].lower() == args.routine.strip().lower()),
                None,
            )
            if match is None:
                print(f"No routine titled '{args.routine}'.", file=sys.stderr)
                return 1
            routine_id = match["id"]
        clean, error = normalize_chore(
            {
                "title": args.add,
                "due_time": args.due,
                "days": args.days.split(",") if args.days else [],
                "owner_id": owner_id,
                "remind_before": args.remind,
                "impact": args.impact,
            }
        )
        if error is not None:
            print(error, file=sys.stderr)
            return 1
        cid = scoped.set_chore(updated_by=scoped.user_id, routine_id=routine_id, **clean)
        when = f"by {fmt_time_12h(clean['due_time'])}" if clean["due_time"] else "untimed"
        into = f" in '{args.routine}'" if routine_id else ""
        print(f"Added chore #{cid}: {clean['title']} ({when}){into}.")
        return 0
    if args.done is not None:
        today = local_datetime(utcnow(), settings.timezone).strftime("%Y-%m-%d")
        result = scoped.log_chore_done(
            chore_id=args.done, done_on=today, done_by=scoped.user_id
        )
        if result is None:
            print(f"No chore #{args.done}.", file=sys.stderr)
            return 1
        print(f"Marked #{args.done} done for today.")
        return 0
    if args.remove is not None:
        if not scoped.remove_chore(args.remove):
            print(f"No chore #{args.remove}.", file=sys.stderr)
            return 1
        print(f"Removed chore #{args.remove}.")
        return 0
    for cid, want in ((args.enable, True), (args.disable, False)):
        if cid is not None:
            if not scoped.set_chore_enabled(cid, want):
                print(f"No chore #{cid}.", file=sys.stderr)
                return 1
            print(f"{'Resumed' if want else 'Paused'} chore #{cid}.")
            return 0

    chores = scoped.chores()
    if not chores:
        print(
            'No shared chores yet. Add one: '
            'household chore --add "run the dishwasher" --due 22:00'
        )
        return 0
    today = local_datetime(utcnow(), settings.timezone).strftime("%Y-%m-%d")
    done_ids = scoped.chore_ids_done_on(today)
    routines_by_id = {r["id"]: r for r in scoped.routines()}
    for c in chores:
        # Show the schedule the chore actually runs on (inherited from its routine
        # unless it sets its own), matching what the reminder sweep uses.
        eff = with_effective_schedule(c, routines_by_id.get(c.get("routine_id")))
        box = "x" if c["id"] in done_ids else " "
        owner = c.get("owner_name") or "either"
        paused = "" if c["enabled"] else " [paused]"
        impact = f" — {c['impact']}" if c.get("impact") else ""
        when = f"by {fmt_time_12h(eff['due_time'])}" if eff.get("due_time") else "untimed"
        routine = f" · {c['routine_title']}" if c.get("routine_title") else ""
        print(
            f"  [{box}] #{c['id']} {c['title']} "
            f"({owner} · {describe_chore_days(eff['days'])} · {when}{routine})"
            f"{paused}{impact}"
        )
    return 0


def _routines_cli(store, args, settings) -> int:
    """List the household's routines, or add/assign-accountable/pause/remove one.

    A routine groups chores under one **accountable** owner (the mental-load
    holder) and carries the schedule its chores inherit. ``--add`` defines one,
    ``--accountable`` sets who holds it, ``--remove`` deletes it (its chores
    survive, unlinked), and ``--enable``/``--disable`` pause or resume it.
    """
    from prefrontal.household import describe_chore_days, fmt_time_12h, normalize_routine

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1

    if args.add:
        accountable_id = None
        if args.accountable:
            who = store.get_user(args.accountable)
            if who is None or who.get("household_id") != scoped.household_id_or_none():
                print(f"'{args.accountable}' isn't a member of this household.", file=sys.stderr)
                return 1
            accountable_id = who["id"]
        clean, error = normalize_routine(
            {
                "title": args.add,
                "due_time": args.due,
                "days": args.days.split(",") if args.days else [],
                "accountable_id": accountable_id,
                "impact": args.impact,
            }
        )
        if error is not None:
            print(error, file=sys.stderr)
            return 1
        rid = scoped.set_routine(updated_by=scoped.user_id, **clean)
        holder = args.accountable if accountable_id else "unassigned"
        print(f"Added routine #{rid}: {clean['title']} (accountable: {holder}).")
        return 0
    if args.remove is not None:
        if not scoped.remove_routine(args.remove):
            print(f"No routine #{args.remove}.", file=sys.stderr)
            return 1
        print(f"Removed routine #{args.remove} (its chores now stand alone).")
        return 0
    for rid, want in ((args.enable, True), (args.disable, False)):
        if rid is not None:
            if not scoped.set_routine_enabled(rid, want):
                print(f"No routine #{rid}.", file=sys.stderr)
                return 1
            print(f"{'Resumed' if want else 'Paused'} routine #{rid}.")
            return 0

    routines = scoped.routines()
    if not routines:
        print(
            'No routines yet. Add one: household routine --add "Monday pickup prep" '
            '--accountable dana --due 07:30'
        )
        return 0
    for r in routines:
        holder = r.get("accountable_name") or "unassigned"
        paused = "" if r["enabled"] else " [paused]"
        when = f"by {fmt_time_12h(r['due_time'])}" if r.get("due_time") else "no set time"
        impact = f" — {r['impact']}" if r.get("impact") else ""
        n = r.get("chore_count") or 0
        print(
            f"  #{r['id']} {r['title']} "
            f"(accountable: {holder} · {describe_chore_days(r['days'])} · {when} · "
            f"{n} chore{'' if n == 1 else 's'}){paused}{impact}"
        )
    return 0


def _chores_check_cli(store, args, settings) -> int:
    """Fire any due chore reminders / miss-handoffs for a household (the sweep, run locally).

    The CLI twin of ``POST /webhooks/household/chores/check`` — for a launchd
    trigger or a manual test. Shares :func:`run_chores_check`, so the notification
    logic lives in one place.
    """
    from prefrontal.household import run_chores_check

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    sent = run_chores_check(scoped, settings=settings)
    if not sent:
        print("No chore reminders due right now.")
        return 0
    for s in sent:
        print(f"  → {s['stage']}: {s['title']} ({len(s['notified'])} notified)")
    return 0


def _balance_cli(store, args, settings) -> int:
    """Print the gentle 'who's keeping the sheet up' balance view for a household."""
    from datetime import timedelta

    from prefrontal.household import BALANCE_WINDOW_DAYS, balance_view

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    if not scoped.is_shared_household():
        print("Single-parent household — nothing to balance.")
        return 0
    since = (utcnow() - timedelta(days=BALANCE_WINDOW_DAYS)).strftime(TS_FMT)
    view = balance_view(
        scoped.contribution_counts(since), carrying=scoped.accountability_counts()
    )
    print(f"Doing — shared work in the last {BALANCE_WINDOW_DAYS} days:")
    for m in view["members"]:
        print(f"  {m['name']}: {m['count']} ({m['share']}%)")
    print(f"  {view['caption']}")
    carrying = view.get("carrying")
    if carrying:
        print("Carrying — routines each parent is accountable for:")
        for m in carrying["members"]:
            print(f"  {m['name']}: {m['count']} ({m['share']}%)")
        print(f"  {carrying['caption']}")
    return 0


def _digest_cli(store, args, settings) -> int:
    """Push each parent the other parent's unseen sheet changes, if the digest is on.

    The CLI twin of ``POST /webhooks/household/digest/check`` — for a launchd
    trigger or a manual test. Self-suppressing: silent per parent when there's
    nothing new or they were digested within the last day.
    """
    from prefrontal.household import (
        digest_interval_ok,
        digest_message,
        unseen_changes,
    )
    from prefrontal.integrations.delivery import (
        deliver_to_member,
        household_digest_notice,
    )

    scoped = _resolve_user_store(store, args.user)
    hid = scoped.household_id_or_none()
    if hid is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    if not scoped.is_shared_household():
        print("Single-parent household — the delta digest is off.")
        return 0
    if not scoped.get_digest_enabled():
        print("The daily digest is off for this household.")
        return 0
    now = utcnow()
    now_str = now.strftime(TS_FMT)
    sent = 0
    for member in scoped.household_members(hid):
        if member.get("status") not in (None, "active"):
            continue
        m_store = store.scoped(member["id"])
        digested = m_store.get_state("household_digested_at")
        since = max(m_store.get_state("household_seen_at") or "", digested or "")
        changes = unseen_changes(m_store, viewer_id=member["id"], since=since)
        if not changes or not digest_interval_ok(digested, now):
            continue
        row = deliver_to_member(
            m_store,
            household_digest_notice(digest_message(changes), channel="push"),
            handle=member["handle"], settings=settings,
            base_url=settings.oauth_base_url, secret=settings.session_secret,
        )
        m_store.set_state("household_digested_at", now_str, source="inferred")
        state = "sent" if row["delivered"] else "not sent"
        print(f"  → {member['handle']}: {len(changes)} change(s) — {state} ({row['detail']})")
        sent += 1
    if not sent:
        print("No digests to send (everyone's caught up or recently digested).")
    return 0


def _checkin_cli(store, args, settings) -> int:
    """Send the weekly mental-load check-in to both parents if it's due.

    The CLI twin of ``POST /webhooks/household/checkin/check`` — for a launchd
    trigger or a manual test. No-op (with a message) when the check-in is off or
    already sent this week.
    """
    from prefrontal.household import checkin_due, checkin_question
    from prefrontal.integrations.delivery import (
        deliver_to_household,
        household_checkin_notice,
    )
    from prefrontal.scheduling import local_datetime

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    if not scoped.is_shared_household():
        print("Single-parent household — the load check-in is off.")
        return 0
    now_local = local_datetime(utcnow(), settings.timezone)
    config = scoped.get_checkin_config()
    last_local = None
    if config.get("last_sent_at"):
        last_local = local_datetime(_parse_last(config["last_sent_at"]), settings.timezone)
    if not checkin_due(config, now_local=now_local, last_sent_local=last_local):
        print("Weekly check-in not due right now (off, wrong day/time, or already sent).")
        return 0
    rows = deliver_to_household(
        store, scoped.household_id_or_none(),
        household_checkin_notice(checkin_question(), channel="push"),
        settings=settings,
        base_url=settings.oauth_base_url, secret=settings.session_secret,
    )
    scoped.mark_checkin_sent()
    for row in rows:
        state = "sent" if row["delivered"] else "not sent"
        print(f"  → {row['handle']}: {state} ({row['detail']})")
    print("Weekly check-in sent to both parents.")
    return 0


def _award_stars_cli(store, args, settings) -> int:
    """Award stars from the CLI, congratulating both parents when a goal is hit.

    Mirrors ``POST /household/agreements/{id}/stars`` — same shared service, so the
    "crossed a reward line → tell both parents" rule stays in one place. The
    operator picks the acting parent with ``--user`` (attributed the grant, signs
    the push).
    """
    from prefrontal.household import award_stars_and_notify

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    if args.count == 0:
        print("Nothing to award (count is 0).", file=sys.stderr)
        return 1
    try:
        result = award_stars_and_notify(
            scoped, args.agreement, delta=args.count,
            awarded_by=scoped.user_id, note=args.note, settings=settings,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if result is None:
        print(f"No agreement with id {args.agreement} in this household.", file=sys.stderr)
        return 1
    print(f"{result['agreement']['title']}: {result['delta']:+d} → {result['total']} total.")
    for goal in result["goals_reached"]:
        print(f"🎉 Goal reached: reward unlocked — {goal['reward']}!")
    for row in result["notified"]:
        state = "sent" if row["delivered"] else "not sent"
        print(f"  → {row['handle']}: {state} ({row['detail']})")
    if result["next_goal"]:
        print(f"Next: {result['next_goal']['remaining']} to go → {result['next_goal']['reward']}.")
    return 0


def _star_prompts_cli(store, args, settings) -> int:
    """Fire any due star-award prompts for a household (the sweep, run locally).

    The CLI twin of ``POST /webhooks/household/star-prompts/check`` — for a launchd
    ``StartCalendarInterval`` trigger or a manual test. Sends both parents a
    one-tap "did <kid> earn a star?" push for each chart whose schedule is due now,
    and marks it so it fires once per local day.
    """
    from prefrontal.household import (
        parse_structured,
        prompt_due,
        prompt_question,
    )
    from prefrontal.integrations.delivery import (
        deliver_to_household,
        household_prompt_notice,
    )
    from prefrontal.scheduling import local_datetime

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    now_local = local_datetime(utcnow(), settings.timezone)
    hid = scoped.household_id_or_none()
    sent = 0
    for agreement in scoped.agreements():
        structured = parse_structured(agreement.get("structured"))
        last = agreement.get("last_prompted_at")
        last_local = local_datetime(_parse_last(last), settings.timezone) if last else None
        if not prompt_due(structured, now_local=now_local, last_prompted_local=last_local):
            continue
        child_name = next(
            (c["name"] for c in scoped.children() if c["id"] == (agreement.get("child_id") or 0)),
            None,
        )
        question = prompt_question(structured, child_name, agreement["title"])
        deliver_to_household(
            store, hid,
            household_prompt_notice(question, agreement["id"], channel="push"),
            settings=settings,
            base_url=settings.oauth_base_url, secret=settings.session_secret,
        )
        scoped.mark_prompted(agreement["id"])
        print(f"Prompted: {agreement['title']} — “{question}”")
        sent += 1
    if not sent:
        print("No prompts due right now.")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    """Upgrade a single-tenant database to the multi-tenant schema in place.

    Non-destructive and idempotent: creates a legacy user, backfills every
    per-user row to it, and prints the legacy user's one-time token. Re-running
    on an already-migrated database is a no-op.

    Args:
        args: Parsed arguments; uses ``db_path`` and optional ``handle``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    # Open the existing file as-is and migrate its legacy shape first (the
    # migration is self-contained — it must run before schema.sql, whose indexes
    # reference user_id). Then apply schema.sql to add the new composite indexes.
    # init_db would also auto-migrate, but here we surface the token + row counts.
    from prefrontal.memory.db import SCHEMA_PATH, connect

    conn = connect(db_path)
    try:
        result = migrate_to_multi_tenant(conn, handle=args.handle)
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()
    if not result.migrated:
        print(f"{db_path} is already multi-tenant — nothing to do.")
        return 0
    total = sum((result.rows_backfilled or {}).values())
    print(f"Migrated {db_path} to multi-tenant.")
    print(f"Legacy user '{result.legacy_handle}' (operator) owns {total} rows.")
    print("Token (shown once):")
    print(f"  {result.token}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the FastAPI webhook listener via uvicorn.

    Args:
        args: Parsed arguments; uses ``args.host`` / ``args.port`` overrides.

    Returns:
        Process exit code (0 on clean shutdown).
    """
    import uvicorn

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    if settings.default_user:
        print(
            f"WARNING: PREFRONTAL_DEFAULT_USER='{settings.default_user}' — tokenless "
            "requests resolve to that user. Only expose this on a trusted network.",
            file=sys.stderr,
        )
    print(f"Serving Prefrontal webhooks on http://{host}:{port} (docs at /docs)")
    uvicorn.run("prefrontal.webhooks.app:app", host=host, port=port, reload=args.reload)
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    """Pull the latest code, reinstall + migrate, then restart the service.

    Runs on the host (the CLI process is separate from the service), so it is
    always allowed — the ``PREFRONTAL_SELF_UPDATE`` gate only guards the HTTP
    surface. Prints the update output; the restart is spawned detached.
    """
    from prefrontal.selfupdate import run_update

    settings = get_settings()
    report = run_update(settings, restart=not args.no_restart)
    up = report["update"]
    print(f"$ {' '.join(up['cmd'])}")
    if up["output"]:
        print(up["output"])
    if not up["ok"]:
        print(f"update failed (exit {up['code']}) — restart skipped.", file=sys.stderr)
        return 1
    if report["restarted"]:
        print(f"restart triggered: {' '.join(report['restart']['cmd'])}")
    else:
        print("update complete (no restart requested).")
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    """Restart the service without updating (detached restart)."""
    from prefrontal.selfupdate import run_restart

    report = run_restart(get_settings())
    print(f"restart triggered: {' '.join(report['restart']['cmd'])}")
    return 0


def _cmd_n8n(args: argparse.Namespace) -> int:
    """Manage the n8n orchestration layer (currently: push workflow templates).

    ``push`` upserts ``deploy/n8n/*.json`` into the running n8n via its REST API
    — the "update n8n directly" step that ``deploy/update.sh`` runs so the
    dashboard Update button syncs workflows too. Skips cleanly (exit 0) when the
    n8n API isn't configured, so it's safe to wire unconditionally into updates.
    """
    from prefrontal.integrations.n8n import DEFAULT_WORKFLOW_DIR, N8nWorkflowSyncer

    if args.n8n_action != "push":  # argparse requires the subcommand, so this is unreachable
        return 1
    syncer = N8nWorkflowSyncer.from_settings(get_settings())
    report = syncer.push(args.dir or DEFAULT_WORKFLOW_DIR, activate=not args.no_activate)
    if not report["enabled"]:
        print(report["detail"])  # no-op skip is a success
        return 0
    for wf in report["pushed"]:
        flag = "ok" if wf["ok"] else "FAIL"
        state = ""
        if wf.get("active") is not None:
            state = " → active" if wf["active"] else " → inactive"
        print(f"[{flag}] {wf['action']:<7} {wf['name']}{state}")
        if not wf["ok"]:
            print(f"        {wf['detail']}", file=sys.stderr)
    if not report["ok"]:
        print(report["detail"], file=sys.stderr)
        return 1
    print(report["detail"] + " to n8n")
    return 0


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
        if args.all_users:
            targets = [(u["handle"], store.scoped(u["id"])) for u in store.each_user()]
        else:
            scoped = _resolve_user_store(store, args.user)
            targets = [(scoped.user_id, scoped)]
        for label, s in targets:
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
            if summary.type_bias:
                types = ", ".join(f"{t}={v}" for t, v in sorted(summary.type_bias.items()))
                print(f"[{label}] task-type bias -> {types}")
            if summary.energy_bias:
                energies = ", ".join(f"{e}={v}" for e, v in sorted(summary.energy_bias.items()))
                print(f"[{label}] energy bias -> {energies}")
            if summary.category_bias:
                cats = ", ".join(f"{c}={v}" for c, v in sorted(summary.category_bias.items()))
                print(f"[{label}] category bias -> {cats}")
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
                cverdict = "helping" if ccal.helps else "NOT helping — channel signal is noise"
                print(
                    f"[{label}] channel check: error {ccal.baseline_error} -> "
                    f"{ccal.adjusted_error} on {ccal.samples} recent ({cverdict})"
                )
            # Sensor precision (learning §2 feedback): are the LLM sensor's
            # proposals worth keeping? Persists the verdict + flags chronically
            # rejected targets, which the extraction prompt then de-emphasizes.
            from prefrontal.sensor import recompute_sensor_calibration

            sc = recompute_sensor_calibration(s)
            if sc.status == "ok":
                flagged = (
                    f"; chronically rejected: {', '.join(sc.flagged)}" if sc.flagged else ""
                )
                print(
                    f"[{label}] sensor precision: {sc.accepted}/{sc.resolved} accepted "
                    f"({sc.accept_rate}){flagged}"
                )
            for c in adapt_self_care(s):
                arrow = "->" if c["changed"] else "="
                print(
                    f"[{label}] self-care {c['check']} interval {arrow} "
                    f"{c['interval']}m ({c['reason']})"
                )
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    """Build the behavioral profile and print it (or write it to a file).

    Args:
        args: Parsed arguments; uses ``args.db_path`` and optional ``args.output``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    # Don't re-seed here; just read whatever exists. initialize=True is still
    # safe and idempotent, and guarantees the tables exist for a fresh checkout.
    with MemoryStore.open(db_path) as store:
        profile = build_profile(_resolve_user_store(store, args.user))
    if args.output:
        Path(args.output).write_text(profile)
        print(f"Wrote profile to {args.output}")
    else:
        print(profile, end="")
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
        if args.all_users:
            targets = [(u["handle"], store.scoped(u["id"])) for u in store.each_user()]
        else:
            scoped = _resolve_user_store(store, args.user)
            handle = next(
                u["handle"] for u in store.list_users() if u["id"] == scoped.user_id
            )
            targets = [(handle, scoped)]
        for handle, s in targets:
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


def _cmd_briefing(args: argparse.Namespace) -> int:
    """Print today's morning briefing (deterministic, or LLM prose with --llm).

    Args:
        args: Parsed arguments; uses ``db_path``, ``llm``, ``output``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.integrations import ProviderResolver

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        if args.llm:
            # Claude when the ``briefing`` agent is opted into Anthropic, else local.
            client = ProviderResolver.from_settings(settings).client("briefing")
            result = summarize_briefing(store, client=client)
            text = result.text
            if result.source == "heuristic":
                print(
                    "Ollama unavailable; printing the structured briefing.",
                    file=sys.stderr,
                )
        else:
            text = render_briefing(build_briefing(store))
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote briefing to {args.output}")
    else:
        print(text, end="")
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
        store = _resolve_user_store(unscoped, args.user)
        now = utcnow()
        ctx = build_context(store, now=now, timezone=settings.timezone)
        modules = enabled_modules(settings)
        cues = collect_cues(store, modules, ctx)
        if args.dry_run:
            if not cues:
                print("No cues due.")
            for c in cues:
                print(f"[{c.urgency}] {c.module}/{c.intervention}: {c.text}")
            return 0
        # Close out prior nudges whose ack window lapsed unanswered → channel
        # "misses" that shift future channel choice (spec §8). Off the dry-run
        # path so a debug run never mutates outcome state.
        swept = sweep_stale_nudges(
            store,
            now,
            ack_window_minutes=store.get_float(
                "coach_ack_window_minutes", DEFAULT_ACK_WINDOW_MINUTES
            ),
        )
        if swept:
            print(f"({swept} unanswered nudge(s) logged as channel misses)")
        # Self-care nudges track latency separately; sweep their unanswered ones
        # into "ignored" episodes so the cadence learner sees "too frequent".
        ignored = sweep_unanswered_self_care(store, now)
        if ignored:
            print(f"({ignored} unanswered self-care nudge(s) logged as ignored)")
        decisions = decide(store, cues, ctx)
        if not decisions:
            print(f"Nothing to say right now ({len(cues)} cue(s) held).")
            return 0
        for d in decisions:
            print(f"[{d.channel}] {d.cue.module}/{d.cue.intervention}: {d.text}")
        if args.deliver:
            _deliver_decisions(unscoped, store, decisions, settings)
        record_fired(store, decisions, now)
        # Track interactive nudges so a tap (or the next sweep) records the channel.
        note_delivered(store, decisions, now)
        # Stamp self-care delivery time for the adaptive-cadence latency signal.
        mark_self_care_prompted(store, decisions, now)
    return 0


def _deliver_decisions(unscoped, store, decisions, settings) -> None:
    """Publish fired decisions via the native delivery client and print outcomes.

    Resolves the acting user's routing (per-user ``coaching_state`` over operator
    defaults) and delivers each decision on its chosen channel. Signs the one-tap
    action buttons with the user's handle so a background tap authenticates.
    """
    from prefrontal.integrations.delivery import DeliveryClient, resolve_route

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


def _cmd_note(args: argparse.Namespace) -> int:
    """Feed a free-text note to the LLM sensor; store any candidates as pending.

    Nothing is written to the profile here — the model only *proposes* structured
    updates, which land as pending proposals for review (``prefrontal proposals``).

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``text``.

    Returns:
        Process exit code (0 on success).
    """
    import json

    from prefrontal.integrations import ProviderResolver
    from prefrontal.sensor import (
        avoided_state_keys,
        extract_candidates,
        extract_candidates_from_transcript,
        record_candidates,
        summarize_candidate,
    )

    settings = get_settings()
    # Claude when the ``sensor`` agent is opted into Anthropic, else local.
    client = ProviderResolver.from_settings(settings).client("sensor")
    # A --transcript file (JSON array of {"speaker","text"} turns) reads a whole
    # conversation; otherwise the positional text is a single note.
    turns: list[dict[str, str]] | None = None
    if getattr(args, "transcript", None):
        try:
            loaded = json.loads(Path(args.transcript).read_text())
        except (OSError, ValueError) as exc:
            print(f"Could not read transcript {args.transcript!r}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(loaded, list):
            print("Transcript file must be a JSON array of turns.", file=sys.stderr)
            return 1
        turns = [t for t in loaded if isinstance(t, dict)]
    elif not (args.text or "").strip():
        print("Provide a note, or --transcript PATH.", file=sys.stderr)
        return 1
    with MemoryStore.open(args.db_path or settings.db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        # Close the calibration loop: de-emphasize settings the user reliably
        # rejects (from the last `prefrontal learn` pass) in the extraction prompt.
        avoid = avoided_state_keys(store)
        if turns is not None:
            candidates = extract_candidates_from_transcript(turns, client=client, avoid_keys=avoid)
        else:
            candidates = extract_candidates(args.text, client=client, avoid_keys=avoid)
        if not candidates:
            print("No candidate updates found (or the model was unreachable).")
            return 0
        ids = record_candidates(store, candidates)
        print(f"Recorded {len(ids)} pending proposal(s) — review with `prefrontal proposals`:")
        for pid, c in zip(ids, candidates, strict=True):
            print(f"  #{pid}  {summarize_candidate(c.kind, c.payload)}")
            if c.rationale:
                print(f"        ↳ {c.rationale}")
    return 0


def _cmd_proposals(args: argparse.Namespace) -> int:
    """List pending sensor proposals, show precision stats, or accept/reject by id.

    Accepting applies the update with ``source='llm_inferred'``; rejecting just
    resolves it. Both only move a still-``pending`` proposal. ``stats`` reports the
    sensor's accept-rate precision (learning §2 feedback) from resolved proposals.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``proposals_action``, ``id``.

    Returns:
        Process exit code (0 on success, 1 if the id isn't a pending proposal).
    """
    from prefrontal.sensor import (
        MIN_SENSOR_CALIBRATION_SAMPLES,
        apply_proposal,
        compute_sensor_calibration,
        summarize_candidate,
    )

    settings = get_settings()
    with MemoryStore.open(args.db_path or settings.db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        action = args.proposals_action
        if action == "list":
            pending = store.list_proposals("pending")
            if not pending:
                print("No pending proposals.")
                return 0
            for p in pending:
                print(f"#{p['id']}  {summarize_candidate(p['kind'], p['payload'])}")
                if p.get("rationale"):
                    print(f"      ↳ {p['rationale']}")
            return 0
        if action == "stats":
            cal = compute_sensor_calibration(store.all_resolved_proposals())
            if cal.status != "ok":
                print(
                    f"Not enough resolved proposals yet ({cal.resolved}; "
                    f"need {MIN_SENSOR_CALIBRATION_SAMPLES}) to judge sensor precision."
                )
                return 0
            print(f"Sensor precision: {cal.accepted}/{cal.resolved} accepted ({cal.accept_rate}).")
            for tp in cal.by_target:
                flag = "  ⚠ chronically rejected" if tp.target in cal.flagged else ""
                print(
                    f"  {tp.target}: {tp.accepted}/{tp.resolved} "
                    f"({round(tp.accept_rate, 2)}){flag}"
                )
            return 0
        # accept / reject
        proposal = store.get_proposal(args.id)
        if proposal is None or proposal["status"] != "pending":
            print(f"No pending proposal #{args.id}.", file=sys.stderr)
            return 1
        if action == "accept":
            applied = apply_proposal(store, proposal)
            store.set_proposal_status(args.id, "accepted")
            print(f"Accepted #{args.id}: {applied} (source=llm_inferred)")
        else:  # reject
            store.set_proposal_status(args.id, "rejected")
            print(f"Rejected #{args.id}.")
    return 0


def _cmd_crunch(args: argparse.Namespace) -> int:
    """Toggle crunch mode — suspend the work/life time bands for a deadline stretch.

    While on, a work todo (or any domain/category) can surface any waking hour;
    the off-zone and travel-late gate still apply. Self-expiring: ``on`` sets a
    ``crunch_until`` timestamp ``--hours`` out, and it lapses on its own.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``crunch_action``, ``hours``.

    Returns:
        Process exit code (0 on success).
    """
    from datetime import timedelta

    from prefrontal.scheduling import _crunch_active

    settings = get_settings()
    with MemoryStore.open(args.db_path or settings.db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        if args.crunch_action == "off":
            store.set_state("crunch_until", "", source="explicit")
            print("Crunch mode off — work/life bands are back on.")
        elif args.crunch_action == "status":
            until = store.get_state("crunch_until")
            if _crunch_active(until):
                print(f"Crunch mode ON until {until} UTC — bands suspended.")
            else:
                print("Crunch mode off.")
        else:  # on
            until = (utcnow() + timedelta(hours=args.hours)).strftime(TS_FMT)
            store.set_state("crunch_until", until, source="explicit")
            print(
                f"Crunch mode ON for ~{args.hours:g}h (until {until} UTC) — "
                "work/life bands suspended; off-zone still applies."
            )
    return 0


def _cmd_place(args: argparse.Namespace) -> int:
    """Add or list curated place aliases (the offline-first geocoding layer).

    A curated place resolves a commitment's free-text ``location`` to coordinates
    instantly and offline (before the cache or the network geocoder), so the
    departure reminder's travel estimate fires. The CLI twin of ``POST`` / ``GET
    /places``.

    Args:
        args: Parsed arguments; ``place_action`` plus action-specific fields.

    Returns:
        Process exit code (0 on success, 1 on an empty name).
    """
    from prefrontal.geocode import normalize_query

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        if args.place_action == "add":
            name = normalize_query(args.name)
            if not name:
                print("Place name is empty after normalization.")
                return 1
            place_id = store.add_place(
                name, args.lat, args.lon, label=args.label or args.name
            )
            print(f"Saved place #{place_id}: {name} ({args.lat:g}, {args.lon:g})")
        elif args.place_action == "list":
            places = store.places()
            if not places:
                print("No curated places yet.")
            for p in places:
                label = p.get("label")
                extra = f" — {label}" if label and label != p["name"] else ""
                print(f"{p['name']}{extra}  ({p['lat']:g}, {p['lon']:g})")
    return 0


def _cmd_todo(args: argparse.Namespace) -> int:
    """Add, list, or close open todos.

    Args:
        args: Parsed arguments; ``todo_action`` plus action-specific fields.

    Returns:
        Process exit code (0 on success, 1 on a not-found close).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        if args.todo_action == "add":
            # Category: explicit flag, else an offline keyword guess — always
            # clamped to the cap against the user's existing set (no LLM here).
            candidate = args.category or heuristic_category(args.title)
            category = resolve_category(candidate, store.todo_categories())
            todo_id = store.add_todo(
                args.title,
                estimate_minutes=args.minutes,
                priority=args.priority,
                energy=args.energy,
                category=category,
            )
            print(f"Added todo #{todo_id} [{category}]: {args.title}")
        elif args.todo_action == "list":
            todos = store.open_todos()
            if not todos:
                print("No open todos.")
            for t in todos:
                est = f" ~{t['estimate_minutes']:g}m" if t.get("estimate_minutes") else ""
                cat = f" ({t['category']})" if t.get("category") else ""
                print(f"#{t['id']} [P{t['priority']}]{est}{cat} {t['title']}")
        elif args.todo_action in ("done", "drop"):
            status_ = "done" if args.todo_action == "done" else "dropped"
            if store.close_todo(args.todo_id, status=status_):
                closed = store.get_todo(args.todo_id)
                if closed is not None:
                    record_todo_closed(store, closed, now=utcnow())
                print(f"Todo #{args.todo_id} marked {status_}.")
            else:
                print(f"Todo #{args.todo_id} is not open.", file=sys.stderr)
                return 1
        elif args.todo_action == "domain":
            domain = (args.domain or "").strip().lower() or None
            if store.set_todo_domain(args.todo_id, domain):
                shown = domain or "(cleared)"
                print(f"Todo #{args.todo_id} domain -> {shown}")
            else:
                print(f"No todo #{args.todo_id}.", file=sys.stderr)
                return 1
    return 0


def _cmd_fit(args: argparse.Namespace) -> int:
    """Show open todos that fit a block of free time, applying the time bias.

    Args:
        args: Parsed arguments; uses ``minutes`` and ``db_path``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.memory.patterns import task_bias_resolver
    from prefrontal.scheduling import local_datetime

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)

        # "right now" → calibrate each todo with this hour's band, its energy /
        # category, then the task-type bias, else global (§5).
        now_hour = local_datetime(utcnow(), settings.timezone).hour
        fits = fit_todos(
            args.minutes, store.open_todos(), bias_fn=task_bias_resolver(store, local_hour=now_hour)
        )
    print(f"With {args.minutes:g} minutes free, you could knock out:")
    if not fits:
        print("  (nothing with an estimate fits — add estimates with `todo add --minutes`)")
    for f in fits:
        t = f["todo"]
        print(f"  #{t['id']} {t['title']} (~{f['effective_minutes']:g}m)")
    return 0


def _cmd_mail(args: argparse.Namespace) -> int:
    """Ingest, fetch, or list triaged mail.

    Subcommands:

    - ``list`` — show recently ingested mail and the open action items.
    - ``sync FILE`` — ingest messages from a JSON file (a list, or an object
      with a ``messages`` key) for the given ``--account``.
    - ``fetch`` — pull unread mail for ``--account`` over IMAP using the
      ``MAIL_IMAP_*_<ACCOUNT>`` env credentials, then ingest it.

    Args:
        args: Parsed arguments; ``mail_action`` plus action-specific fields.

    Returns:
        Process exit code (0 on success, 1 on a usage/credential error).
    """
    import json

    from prefrontal.integrations.ollama import OllamaClient
    from prefrontal.mail import ingest_messages
    from prefrontal.mail.feedback import learned_corrections, learned_denylist

    settings = get_settings()
    db_path = args.db_path or settings.db_path

    if args.mail_action == "learned":
        with MemoryStore.open(db_path) as unscoped:
            store = _resolve_user_store(unscoped, args.user)
            if args.clear:
                cleared = store.clear_triage_feedback()
                print(f"Cleared {cleared} learned triage correction(s).")
                return 0
            senders = store.triage_dropped_senders(
                min_count=settings.triage_repeat_threshold
            )
            recent = store.triage_feedback_list(limit=20)
            addendum = learned_corrections(
                store,
                quick_drop_days=settings.triage_quick_drop_days,
                repeat_threshold=settings.triage_repeat_threshold,
            )
        print(f"Repeat-dropped senders ({len(senders)}):")
        for s in senders:
            who = s.get("sender_email") or s.get("sender_name") or "?"
            print(f"  {who}: {s.get('drops')} dropped")
        print(f"\nRecent drops ({len(recent)}):")
        for r in recent:
            who = r.get("sender_name") or r.get("sender_email") or "?"
            age = r.get("days_open")
            age_s = f"{age:.1f}d" if isinstance(age, (int, float)) else "?"
            print(f"  #{r.get('id')} [{age_s}] {who}: {r.get('subject') or '(no subject)'}")
        if addendum:
            print("\nPrompt addendum injected on the next sync:")
            print(addendum)
        else:
            print("\n(No corrections qualify yet — triage uses the base prompt.)")
        return 0

    if args.mail_action == "retriage":
        from prefrontal.mail import retriage_messages

        client = (
            None
            if args.heuristic
            else OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)
        )
        with MemoryStore.open(db_path) as unscoped:
            store = _resolve_user_store(unscoped, args.user)
            summary = retriage_messages(
                store,
                account=args.account,
                only_needs_action=not args.all_mail,
                client=client,
                use_model=not args.heuristic,
                create_todos=args.create_todos,
                corrections=learned_corrections(
                    store,
                    quick_drop_days=settings.triage_quick_drop_days,
                    repeat_threshold=settings.triage_repeat_threshold,
                ),
                denylisted_senders=learned_denylist(
                    store, repeat_threshold=settings.triage_repeat_threshold
                ),
                dry_run=args.dry_run,
            )
        scope = args.account or "all accounts"
        lead = "[dry-run] would re-triage" if summary.dry_run else "re-triaged"
        print(
            f"[{scope}] {lead} {summary.scanned} message(s): "
            f"{summary.changed} changed, {summary.cleared} cleared "
            f"({summary.todos_dropped} todos dropped), "
            f"{summary.newly_flagged} newly flagged "
            f"({summary.todos_created} todos created, "
            f"{summary.todos_suppressed} suppressed), "
            f"{summary.triaged_by_llm} via model."
        )
        if summary.dry_run:
            print("Nothing was written. Re-run without --dry-run to apply.")
        return 0

    if args.mail_action == "list":
        with MemoryStore.open(db_path) as unscoped:
            store = _resolve_user_store(unscoped, args.user)
            action_items = store.mail_needing_action()
            recent = store.recent_mail(limit=20)
        print(f"Needs action: {len(action_items)}")
        for m in action_items:
            who = m.get("sender_name") or m.get("sender_email") or "?"
            print(f"  [{m.get('urgency', '?')}] {who}: {m.get('subject') or '(no subject)'}")
        print(f"\nRecent ({len(recent)}):")
        for m in recent:
            flag = "*" if m.get("needs_action") else " "
            print(f" {flag} {m.get('category', '?'):12} {m.get('subject') or '(no subject)'}")
        return 0

    # `sync` and `fetch` both ingest. Resolve the policy and an Ollama client.
    account = args.account
    policy = settings.policy_for(account)
    client = OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)

    if args.mail_action == "sync":
        try:
            data = json.loads(Path(args.file).read_text())
        except (OSError, ValueError) as exc:
            print(f"Could not read messages from {args.file}: {exc}", file=sys.stderr)
            return 1
        messages = data.get("messages", []) if isinstance(data, dict) else data
        if not isinstance(messages, list):
            print(
                "Expected a JSON list of messages, or an object with 'messages'.",
                file=sys.stderr,
            )
            return 1
    else:  # fetch
        from prefrontal.mail.imap import ImapAccount, fetch_unread

        imap = ImapAccount.from_env(account)
        if imap is None:
            print(
                f"No IMAP credentials for account '{account}'. Set "
                f"MAIL_IMAP_USER_{account.upper()} and MAIL_IMAP_PASSWORD_{account.upper()}.",
                file=sys.stderr,
            )
            return 1
        try:
            messages = fetch_unread(
                imap, limit=args.limit, since_days=args.since_days
            )
        except Exception as exc:  # imaplib raises a variety of errors
            print(f"IMAP fetch failed: {exc}", file=sys.stderr)
            return 1

    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        summary = ingest_messages(
            store,
            messages,
            account=account,
            policy=policy,
            client=client,
            use_model=not args.heuristic,
            corrections=learned_corrections(
                store,
                quick_drop_days=settings.triage_quick_drop_days,
                repeat_threshold=settings.triage_repeat_threshold,
            ),
            denylisted_senders=learned_denylist(
                store, repeat_threshold=settings.triage_repeat_threshold
            ),
            domain=settings.account_domain_map.get(account),
        )
    print(
        f"[{summary.account}/{summary.policy}] received {summary.received}, "
        f"ingested {summary.ingested}, skipped {summary.skipped}, "
        f"needs-action {summary.needs_action} "
        f"({summary.todos_created} todos, {summary.todos_suppressed} suppressed), "
        f"{summary.triaged_by_llm} via model."
    )
    return 0


def _cmd_modules(args: argparse.Namespace) -> int:
    """List available modules and whether each is enabled.

    Args:
        args: Parsed arguments; uses ``args.verbose`` to also list interventions.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    enabled = {m.key for m in enabled_modules(settings)}
    for module in available():
        mark = "on " if module.key in enabled else "off"
        print(f"[{mark}] {module.key} — {module.title}")
        print(f"        {module.challenge}")
        if args.verbose:
            for iv in module.interventions():
                print(f"          - {iv.name} ({iv.status}): {iv.description}")
    return 0


def _cmd_packs(args: argparse.Namespace) -> int:
    """List available Context Packs and whether each is enabled.

    Args:
        args: Parsed arguments; uses ``args.verbose`` to also list the modules a
            pack switches on and the vocabulary/defaults it seeds.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.packs import available as available_packs
    from prefrontal.packs import is_enabled as pack_is_enabled

    settings = get_settings()
    packs = available_packs()
    if not packs:
        print("No Context Packs registered.")
        return 0
    for pack in packs:
        mark = "on " if pack_is_enabled(pack.key, settings) else "off"
        print(f"[{mark}] {pack.key} — {pack.title}")
        if pack.description:
            print(f"        {pack.description}")
        if args.verbose:
            if pack.modules:
                print(f"          modules: {', '.join(pack.modules)}")
            if pack.categories:
                print(f"          categories: {', '.join(pack.categories)}")
            if pack.commitment_kinds:
                print(f"          commitment kinds: {', '.join(pack.commitment_kinds)}")
            for key, value in pack.coaching_defaults.items():
                print(f"          seeds {key} = {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser` with subcommands attached.
    """
    parser = argparse.ArgumentParser(
        prog="prefrontal",
        description="Prefrontal — an open source executive function agent system.",
    )
    parser.add_argument("--version", action="version", version=f"prefrontal {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Create the SQLite memory database.")
    p_init.add_argument("--db-path", default=None, help="Override the database path.")
    p_init.set_defaults(func=_cmd_init_db)

    p_user = sub.add_parser("user", help="Provision users (add/list/rotate/disable).")
    p_user.add_argument("--db-path", default=None, help="Override the database path.")
    user_sub = p_user.add_subparsers(dest="user_action", required=True)
    u_add = user_sub.add_parser("add", help="Provision a new user (prints a token).")
    u_add.add_argument("handle", help="Unique short handle, e.g. 'sam'.")
    u_add.add_argument("--display-name", default=None, help="Name shown in nudges.")
    u_add.add_argument(
        "--operator", action="store_true", help="Grant admin (operator) rights."
    )
    user_sub.add_parser("list", help="List users (never their tokens).")
    u_rotate = user_sub.add_parser("rotate", help="Mint a new token for a user.")
    u_rotate.add_argument("handle", help="The user's handle.")
    u_disable = user_sub.add_parser("disable", help="Disable a user's access.")
    u_disable.add_argument("handle", help="The user's handle.")
    p_user.set_defaults(func=_cmd_user)

    p_house = sub.add_parser(
        "household",
        help="Manage the shared household sheet (add/join/show/star/prompt-check/checkin-check…).",
    )
    p_house.add_argument("--db-path", default=None, help="Override the database path.")
    house_sub = p_house.add_subparsers(dest="household_action", required=True)
    h_add = house_sub.add_parser("add", help="Create a household (prints its id).")
    h_add.add_argument("name", help="Household name, e.g. 'The Kims'.")
    h_join = house_sub.add_parser("join", help="Put a user into a household.")
    h_join.add_argument("handle", help="The user's handle.")
    h_join.add_argument(
        "--household", type=int, required=True, help="Household id (from `household add`)."
    )
    h_leave = house_sub.add_parser("leave", help="Remove a user from their household.")
    h_leave.add_argument("handle", help="The user's handle.")
    h_show = house_sub.add_parser("show", help="Print the rendered shared sheet.")
    h_show.add_argument("--user", default=None, help="Handle of a household member.")
    h_star = house_sub.add_parser(
        "star", help="Award stars on a chart; congratulate + notify both parents."
    )
    h_star.add_argument(
        "--agreement", type=int, required=True, help="Star-chart agreement id (from `show`)."
    )
    h_star.add_argument(
        "--count", type=int, default=1, help="Stars to award (negative to correct)."
    )
    h_star.add_argument("--note", default=None, help="Optional 'what for' note.")
    h_star.add_argument("--user", default=None, help="Acting parent's handle.")
    h_check = house_sub.add_parser(
        "prompt-check", help="Fire any star-award prompts due now (both parents)."
    )
    h_check.add_argument("--user", default=None, help="Handle of a household member.")
    h_ci = house_sub.add_parser(
        "checkin-check", help="Send the weekly mental-load check-in if it's due."
    )
    h_ci.add_argument("--user", default=None, help="Handle of a household member.")
    h_dig = house_sub.add_parser(
        "digest-check", help="Push each parent the other's unseen sheet changes."
    )
    h_dig.add_argument("--user", default=None, help="Handle of a household member.")
    h_bal = house_sub.add_parser(
        "balance", help="Print the gentle 'who's keeping the sheet up' view."
    )
    h_bal.add_argument("--user", default=None, help="Handle of a household member.")
    h_shop = house_sub.add_parser(
        "shopping", help="List the shared shopping list (or add/check/remove an item)."
    )
    h_shop.add_argument("--user", default=None, help="Handle of a household member.")
    h_shop.add_argument("--add", default=None, help="Add an item (the thing to buy).")
    h_shop.add_argument("--spec", default=None, help="Size / brand / details (with --add).")
    h_shop.add_argument("--where", default=None, help="Where to buy it (with --add).")
    h_shop.add_argument("--child", type=int, default=None, help="A children.id (with --add).")
    h_shop.add_argument("--got", type=int, default=None, help="Check off item by id.")
    h_shop.add_argument("--remove", type=int, default=None, help="Remove item by id.")
    h_shop.add_argument(
        "--clear-got", action="store_true", help="Remove all checked-off items at once."
    )
    h_chore = house_sub.add_parser(
        "chore", help="List shared chores (or add/mark-done/pause/remove one)."
    )
    h_chore.add_argument("--user", default=None, help="Handle of a household member.")
    h_chore.add_argument("--add", default=None, help="Add a chore (what to do).")
    h_chore.add_argument(
        "--due", default=None, help="Due time 'HH:MM' local (omit = inherit routine / untimed)."
    )
    h_chore.add_argument(
        "--days", default=None, help="Weekday CSV '0,1,2' (0=Mon; omit = every day)."
    )
    h_chore.add_argument("--owner", default=None, help="Owner's handle (with --add).")
    h_chore.add_argument(
        "--routine", default=None, help="Routine title to file this chore under (with --add)."
    )
    h_chore.add_argument(
        "--remind", type=int, default=30, help="Minutes before due to nudge (with --add)."
    )
    h_chore.add_argument("--impact", default=None, help="Why it matters if it slips.")
    h_chore.add_argument("--done", type=int, default=None, help="Mark chore done today, by id.")
    h_chore.add_argument("--remove", type=int, default=None, help="Remove chore by id.")
    h_chore.add_argument("--enable", type=int, default=None, help="Resume chore by id.")
    h_chore.add_argument("--disable", type=int, default=None, help="Pause chore by id.")
    h_routine = house_sub.add_parser(
        "routine", help="List routines (or add/assign-accountable/pause/remove one)."
    )
    h_routine.add_argument("--user", default=None, help="Handle of a household member.")
    h_routine.add_argument("--add", default=None, help="Add a routine (its title).")
    h_routine.add_argument(
        "--accountable", default=None, help="Handle of the member who holds the mental load."
    )
    h_routine.add_argument(
        "--due", default=None, help="Time 'HH:MM' its chores inherit (omit = not time-tied)."
    )
    h_routine.add_argument(
        "--days", default=None, help="Weekday CSV '0,1,2' (0=Mon; omit = every day)."
    )
    h_routine.add_argument("--impact", default=None, help="Why the routine matters if it slips.")
    h_routine.add_argument("--remove", type=int, default=None, help="Remove routine by id.")
    h_routine.add_argument("--enable", type=int, default=None, help="Resume routine by id.")
    h_routine.add_argument("--disable", type=int, default=None, help="Pause routine by id.")
    h_chk = house_sub.add_parser(
        "chores-check", help="Fire any chore reminders / miss-handoffs due now."
    )
    h_chk.add_argument("--user", default=None, help="Handle of a household member.")
    h_inv = house_sub.add_parser(
        "invite", help="Generate a shareable invite code for your household."
    )
    h_inv.add_argument("--user", default=None, help="Handle of a household member.")
    h_red = house_sub.add_parser("redeem", help="Join a household with an invite code.")
    h_red.add_argument("code", help="The invite code shared by a co-parent.")
    h_red.add_argument("--user", default=None, help="Handle of the joining user.")
    p_house.set_defaults(func=_cmd_household)

    p_migrate = sub.add_parser(
        "migrate-multi-tenant",
        help="Upgrade a single-tenant DB to multi-tenant (idempotent).",
    )
    p_migrate.add_argument("--db-path", default=None, help="Override the database path.")
    p_migrate.add_argument(
        "--handle",
        default=None,
        help="Handle for the legacy user (default: coaching_state.user_name or 'me').",
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    p_serve = sub.add_parser("serve", help="Run the webhook listener.")
    p_serve.add_argument("--host", default=None, help="Bind host (default from config).")
    p_serve.add_argument("--port", type=int, default=None, help="Bind port (default from config).")
    p_serve.add_argument(
        "--reload", action="store_true", help="Auto-reload on code changes (development)."
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_update = sub.add_parser(
        "update", help="Pull the latest code, reinstall + migrate, then restart the service."
    )
    p_update.add_argument(
        "--no-restart", action="store_true", help="Update only; don't restart the service."
    )
    p_update.set_defaults(func=_cmd_update)

    p_restart = sub.add_parser("restart", help="Restart the service (no code update).")
    p_restart.set_defaults(func=_cmd_restart)

    p_n8n = sub.add_parser("n8n", help="Manage the n8n orchestration layer.")
    n8n_sub = p_n8n.add_subparsers(dest="n8n_action", required=True)
    n8n_push = n8n_sub.add_parser(
        "push",
        help="Upsert deploy/n8n/*.json into the running n8n via its REST API.",
    )
    n8n_push.add_argument(
        "--dir", default=None, help="Workflow template directory (default: deploy/n8n)."
    )
    n8n_push.add_argument(
        "--no-activate",
        action="store_true",
        help="Upsert definitions only; don't converge each workflow's active state.",
    )
    p_n8n.set_defaults(func=_cmd_n8n)

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

    p_brief = sub.add_parser("briefing", help="Print today's morning briefing.")
    p_brief.add_argument("--db-path", default=None, help="Override the database path.")
    p_brief.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_brief.add_argument(
        "--llm", action="store_true", help="Rewrite as prose via Ollama (falls back)."
    )
    p_brief.add_argument("-o", "--output", default=None, help="Write to a file instead of stdout.")
    p_brief.set_defaults(func=_cmd_briefing)

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
        "--dry-run",
        action="store_true",
        help="Show cues before suppression/channel choice; record nothing.",
    )
    p_coach.add_argument(
        "--deliver",
        action="store_true",
        help="Actually publish each fired decision via ntfy/Pushover/TTS (else just print).",
    )
    p_coach.set_defaults(func=_cmd_coach)

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

    p_note = sub.add_parser(
        "note", help="Feed a free-text note to the LLM sensor (proposes, never writes)."
    )
    p_note.add_argument(
        "text",
        nargs="?",
        default="",
        help="The observation, e.g. 'I always blow off admin on Mondays'.",
    )
    p_note.add_argument(
        "--transcript",
        default=None,
        metavar="PATH",
        help="Read a conversation instead: a JSON array of {\"speaker\",\"text\"} turns.",
    )
    p_note.add_argument("--db-path", default=None, help="Override the database path.")
    p_note.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_note.set_defaults(func=_cmd_note)

    p_proposals = sub.add_parser(
        "proposals", help="Review LLM-sensor candidates: list / accept / reject."
    )
    p_proposals.add_argument("--db-path", default=None, help="Override the database path.")
    p_proposals.add_argument("--user", default=None, help="Handle of the user to act on.")
    prop_sub = p_proposals.add_subparsers(dest="proposals_action", required=True)
    prop_sub.add_parser("list", help="List pending proposals.")
    prop_sub.add_parser(
        "stats", help="Show the sensor's accept-rate precision (overall + per target)."
    )
    p_prop_accept = prop_sub.add_parser("accept", help="Apply a proposal (source=llm_inferred).")
    p_prop_accept.add_argument("id", type=int, help="Proposal id.")
    p_prop_reject = prop_sub.add_parser("reject", help="Discard a proposal.")
    p_prop_reject.add_argument("id", type=int, help="Proposal id.")
    p_proposals.set_defaults(func=_cmd_proposals)

    p_crunch = sub.add_parser(
        "crunch", help="Suspend work/life time bands for a deadline stretch (on/off/status)."
    )
    p_crunch.add_argument("--db-path", default=None, help="Override the database path.")
    p_crunch.add_argument("--user", default=None, help="Handle of the user to act on.")
    crunch_sub = p_crunch.add_subparsers(dest="crunch_action", required=True)
    c_on = crunch_sub.add_parser("on", help="Turn crunch on for N hours.")
    c_on.add_argument("--hours", type=float, default=48.0, help="How long to stay in crunch.")
    crunch_sub.add_parser("off", help="Turn crunch off now.")
    crunch_sub.add_parser("status", help="Show whether crunch is on.")
    p_crunch.set_defaults(func=_cmd_crunch)

    p_todo = sub.add_parser("todo", help="Add/list/close open todos (open loops).")
    p_todo.add_argument("--db-path", default=None, help="Override the database path.")
    p_todo.add_argument("--user", default=None, help="Handle of the user to act on.")
    todo_sub = p_todo.add_subparsers(dest="todo_action", required=True)
    t_add = todo_sub.add_parser("add", help="Add a todo.")
    t_add.add_argument("title", help="What needs doing.")
    t_add.add_argument("--minutes", type=float, default=None, help="Time estimate.")
    t_add.add_argument(
        "--priority", type=int, default=1, choices=[0, 1, 2, 3], help="0 low … 3 urgent."
    )
    t_add.add_argument("--energy", default=None, help="low | medium | high.")
    t_add.add_argument(
        "--category", default=None, help="Topic label. Omit to infer (keyword guess)."
    )
    todo_sub.add_parser("list", help="List open todos.")
    t_done = todo_sub.add_parser("done", help="Mark a todo done.")
    t_done.add_argument("todo_id", type=int)
    t_drop = todo_sub.add_parser("drop", help="Drop a todo.")
    t_drop.add_argument("todo_id", type=int)
    t_domain = todo_sub.add_parser("domain", help="Set/clear a todo's work/home domain.")
    t_domain.add_argument("todo_id", type=int)
    t_domain.add_argument(
        "domain", nargs="?", default=None, help="work / home / … (omit to clear)."
    )
    p_todo.set_defaults(func=_cmd_todo)

    p_place = sub.add_parser(
        "place", help="Add/list curated place aliases (offline commitment geocoding)."
    )
    p_place.add_argument("--db-path", default=None, help="Override the database path.")
    p_place.add_argument("--user", default=None, help="Handle of the user to act on.")
    place_sub = p_place.add_subparsers(dest="place_action", required=True)
    pl_add = place_sub.add_parser("add", help="Add or update a curated place alias.")
    pl_add.add_argument("name", help="Alias matched in a location, e.g. 'gym'.")
    pl_add.add_argument("lat", type=float, help="Latitude in degrees.")
    pl_add.add_argument("lon", type=float, help="Longitude in degrees.")
    pl_add.add_argument("--label", default=None, help="Display label (defaults to the name).")
    place_sub.add_parser("list", help="List curated places (most specific first).")
    p_place.set_defaults(func=_cmd_place)

    p_fit = sub.add_parser("fit", help="Show todos that fit a block of free time.")
    p_fit.add_argument("minutes", type=float, help="Minutes of free time you have.")
    p_fit.add_argument("--db-path", default=None, help="Override the database path.")
    p_fit.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_fit.set_defaults(func=_cmd_fit)

    p_mail = sub.add_parser("mail", help="Ingest/fetch/list triaged email.")
    p_mail.add_argument("--db-path", default=None, help="Override the database path.")
    p_mail.add_argument("--user", default=None, help="Handle of the user to act on.")
    mail_sub = p_mail.add_subparsers(dest="mail_action", required=True)
    mail_sub.add_parser("list", help="List recent triaged mail and action items.")
    m_learned = mail_sub.add_parser(
        "learned", help="Show (or clear) what triage learned from dropped todos."
    )
    m_learned.add_argument(
        "--clear", action="store_true", help="Forget all learned corrections."
    )
    m_retriage = mail_sub.add_parser(
        "retriage",
        help="Re-run triage on already-ingested mail with the current prompt.",
    )
    m_retriage.add_argument(
        "--account", default=None, help="Limit to one account (default: all)."
    )
    m_retriage.add_argument(
        "--all",
        dest="all_mail",
        action="store_true",
        help="Re-triage every stored message, not just current needs-action items "
        "(can also newly flag previously-cleared mail).",
    )
    m_retriage.add_argument(
        "--heuristic",
        action="store_true",
        help="Skip the model; re-triage with the keyword heuristic.",
    )
    m_retriage.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    m_retriage.add_argument(
        "--no-todos",
        dest="create_todos",
        action="store_false",
        help="Don't create todos for newly-flagged mail (with --all).",
    )
    m_sync = mail_sub.add_parser("sync", help="Ingest messages from a JSON file.")
    m_sync.add_argument("file", help="Path to a JSON list (or {messages: [...]}).")
    m_sync.add_argument("--account", required=True, help="Logical account name.")
    m_sync.add_argument(
        "--heuristic",
        action="store_true",
        help="Skip the model; triage with the keyword heuristic (fast backlog clear).",
    )
    m_fetch = mail_sub.add_parser("fetch", help="Fetch unread over IMAP, then ingest.")
    m_fetch.add_argument("--account", required=True, help="Logical account name.")
    m_fetch.add_argument("--limit", type=int, default=50, help="Max unread to fetch.")
    m_fetch.add_argument(
        "--since-days",
        type=int,
        default=DEFAULT_UNSEEN_WINDOW_DAYS,
        help="Only consider unread newer than N days (0 = all). Bounds big inboxes.",
    )
    m_fetch.add_argument(
        "--heuristic",
        action="store_true",
        help="Skip the model; triage with the keyword heuristic (fast backlog clear).",
    )
    p_mail.set_defaults(func=_cmd_mail)

    p_packs = sub.add_parser("packs", help="List Context Packs and their status.")
    p_packs.add_argument(
        "-v", "--verbose", action="store_true",
        help="Also list the modules, vocabulary, and defaults each pack contributes.",
    )
    p_packs.set_defaults(func=_cmd_packs)

    p_modules = sub.add_parser("modules", help="List challenge-area modules and their status.")
    p_modules.add_argument(
        "-v", "--verbose", action="store_true", help="Also list each module's interventions."
    )
    p_modules.set_defaults(func=_cmd_modules)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Program entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
