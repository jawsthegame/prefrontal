"""Command line entry point for Prefrontal.

Exposes subcommands, wired up as the ``prefrontal`` console script in
``pyproject.toml``:

- ``prefrontal init-db`` — create the SQLite memory database.
- ``prefrontal user`` — provision users (add/list/rotate/disable/route/connect-link).
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
from prefrontal.clarify import MAX_SWEEP_ITEMS
from prefrontal.clock import TS_FMT
from prefrontal.coaching import (
    build_context,
    collect_cues,
)
from prefrontal.config import get_settings
from prefrontal.encouragement import (
    assess_day,
    build_recovery,
    encouragement_cues,
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
from prefrontal.modules.hyperfocus import adapt_soft_block
from prefrontal.modules.self_care import (
    adapt_self_care,
)
from prefrontal.modules.time_blindness import adapt_morning_routine
from prefrontal.panic import build_panic, render_panic, summarize_panic
from prefrontal.scheduling import fit_todos
from prefrontal.todos import (
    heuristic_category,
    reclassify_hygiene_drops,
    record_todo_closed,
    resolve_category,
)


def _resolve_user_store(store: MemoryStore, handle: str | None) -> MemoryStore:
    """Return ``store`` scoped to a user chosen by ``handle`` (or the only one).

    Handle matching is case-insensitive (an exact-case match still wins), so a
    launchd/cron ``--user tom`` resolves the ``Tom`` account instead of silently
    dropping the tick — the casing slip that once left the coach agent delivering
    to no one.

    Args:
        store: An unscoped store.
        handle: The user's handle, or ``None`` to auto-pick when exactly one
            user exists.

    Returns:
        A store scoped to the resolved user.

    Raises:
        SystemExit: With a clear message if the handle is unknown (or matches
            more than one account only by case), or if no handle was given and
            zero/many users exist.
    """
    users = store.list_users()
    if handle is not None:
        match = next((u for u in users if u["handle"] == handle), None)
        if match is None:
            # Fall back to a case-insensitive match (handles are UNIQUE but
            # case-sensitively, so guard against two case-variant accounts).
            ci = [u for u in users if u["handle"].lower() == handle.lower()]
            if len(ci) > 1:
                names = ", ".join(u["handle"] for u in ci)
                raise SystemExit(
                    f"Ambiguous user '{handle}' — matches {names} by case; "
                    "pass the exact handle."
                )
            match = ci[0] if ci else None
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


def _user_targets(
    store: MemoryStore, args: argparse.Namespace
) -> list[tuple[str, MemoryStore]]:
    """Resolve the ``(handle, scoped_store)`` pairs a data command should act on.

    With ``--all-users`` (for commands that define it) this is every active user;
    otherwise the single user named by ``--user`` (or the sole user). The handle
    is only a label for output. ``store`` must be unscoped.
    """
    if getattr(args, "all_users", False):
        return [(u["handle"], store.scoped(u["id"])) for u in store.each_user()]
    scoped = _resolve_user_store(store, args.user)
    handle = next(
        (u["handle"] for u in store.list_users() if u["id"] == scoped.user_id),
        str(scoped.user_id),
    )
    return [(handle, scoped)]


def _cmd_secrets(args: argparse.Namespace) -> int:
    """Manage the Fernet key that seals per-user source credentials at rest.

    ``status`` reports whether a usable key is configured (never prints it);
    ``init`` mints one when none exists and prints setup + backup guidance.

    Args:
        args: Parsed arguments; uses ``secrets_action``.

    Returns:
        Process exit code (0 on success, 1 when no key is configured for
        ``status``).
    """
    from prefrontal.crypto import generate_key, secret_key_configured

    settings = get_settings()
    if args.secrets_action == "status":
        if secret_key_configured(settings):
            print("Secret key: configured — source secrets can be sealed/opened.")
            return 0
        print(
            "Secret key: NOT configured. Run `prefrontal secrets init`.",
            file=sys.stderr,
        )
        return 1
    if args.secrets_action == "init":
        if secret_key_configured(settings):
            print("A secret key is already configured; nothing to do.")
            print(
                "(Rotating it would orphan every sealed secret — re-enter mail "
                "creds / re-authorize Google instead.)"
            )
            return 0
        key = generate_key()
        print("Generated a new Fernet secret key. Add it to your environment:")
        print()
        print(f"  PREFRONTAL_SECRET_KEY={key}")
        print()
        print(
            "Store it somewhere durable and BACK IT UP — losing it makes every "
            "sealed secret\n(IMAP passwords, Google refresh tokens) unrecoverable."
        )
        return 0
    return 1


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


def build_connect_link(
    base_url: str,
    *,
    token: str | None = None,
    ntfy_server: str | None = None,
    ntfy_topic: str | None = None,
    handle: str | None = None,
    display_name: str | None = None,
) -> str:
    """Build the ``prefrontal://connect?…`` deep link the iOS app consumes.

    This is the operator→phone handoff: rendered as a QR on the setup sheet, it
    lets a new phone connect by pointing its camera (iOS Camera recognises the
    custom scheme) rather than hand-typing a base URL and a long token. The iOS
    parser (``ios/Prefrontal/Onboarding/ConnectPayload.swift``) reads the same
    query keys; keep the two in sync.

    Only ``base_url`` is required. ``token`` is omitted when unknown (the user
    pastes it), and the ntfy hints just prefill the notifications step.

    Args:
        base_url: The deployment origin, e.g. ``https://agent-1.tail….ts.net``.
        token: The user's ``X-Prefrontal-Token`` (embedded only when known).
        ntfy_server: ntfy server the topic lives on.
        ntfy_topic: The user's own ntfy topic.
        handle: The user's handle (advisory).
        display_name: Name shown on the app's "you're all set" screen.

    Returns:
        A ``prefrontal://connect?…`` URL with percent-encoded query values.
    """
    from urllib.parse import urlencode

    params: list[tuple[str, str]] = [("url", base_url.rstrip("/"))]
    if token:
        params.append(("token", token))
    if ntfy_server:
        params.append(("ntfy_server", ntfy_server))
    if ntfy_topic:
        params.append(("ntfy_topic", ntfy_topic))
    if handle:
        params.append(("handle", handle))
    if display_name:
        params.append(("name", display_name))
    return "prefrontal://connect?" + urlencode(params)


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
            if args.email and store.get_user_by_email(args.email) is not None:
                print(f"Email '{args.email}' is already used.", file=sys.stderr)
                return 1
            user, token = provision_user(
                store,
                args.handle,
                display_name=args.display_name or args.handle,
                is_operator=args.operator,
                email=args.email,
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
                email = f" <{u['email']}>" if u.get("email") else ""
                print(
                    f"{u['handle']} ({u['status']}){op}{email} — {u['display_name'] or ''}"
                )
        elif args.user_action == "email":
            try:
                changed = store.set_user_email(args.handle, args.email)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if not changed:
                print(f"No such user '{args.handle}'.", file=sys.stderr)
                return 1
            current = store.get_user(args.handle)["email"]
            print(
                f"Set '{args.handle}' email to {current}."
                if current
                else f"Cleared '{args.handle}' email."
            )
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
        elif args.user_action == "route":
            from prefrontal.integrations.delivery import resolve_route

            scoped = _resolve_user_store(store, args.handle)  # SystemExits if unknown
            # Only the flags actually passed are written; an empty string clears a
            # field (resolve_route treats "" as unset and falls back). All keys are
            # written source="explicit" so the coaching learner never overwrites a
            # deliberately-set route.
            fields = {
                "ntfy_topic": args.ntfy_topic,
                "ntfy_server": args.ntfy_server,
                "ntfy_token": args.ntfy_token,
                "pushover_user_key": args.pushover_user_key,
                "pushover_token": args.pushover_token,
            }
            changed = {k: v for k, v in fields.items() if v is not None}
            for key, value in changed.items():
                scoped.set_state(key, value.strip(), source="explicit")
            route = resolve_route(scoped, settings)
            shown = lambda s: "set" if s else "—"  # noqa: E731 — never print secret values
            topic = route.ntfy_topic or "(none — nudges go nowhere)"
            print(f"Delivery route for '{args.handle}':")
            print(f"  ntfy        {route.ntfy_server}/{topic}")
            print(f"  ntfy token  {shown(route.ntfy_token)}")
            print(
                f"  pushover    user_key {shown(route.pushover_user_key)} · "
                f"token {shown(route.pushover_token)}"
            )
            if changed:
                print(
                    f"Updated: {', '.join(sorted(changed))}. "
                    f"Verify with `prefrontal notify --user {args.handle}`."
                )
        elif args.user_action == "connect-link":
            from prefrontal.integrations.delivery import resolve_route

            user = store.get_user(args.handle)
            if user is None:
                print(f"No such user '{args.handle}'.", file=sys.stderr)
                return 1
            base_url = (args.base_url or settings.oauth_base_url or "").rstrip("/")
            if not base_url:
                print(
                    "No base URL. Set OAUTH_BASE_URL in .env or pass --base-url "
                    "https://<host>.ts.net.",
                    file=sys.stderr,
                )
                return 1
            # A token is shown once at provisioning, so we can't re-read it here.
            # --rotate mints a fresh one to embed (invalidating the old); without
            # it the link carries no token and the user pastes theirs in-app.
            token = store.rotate_user_token(args.handle) if args.rotate else None
            route = resolve_route(store.scoped(user["id"]), settings)
            link = build_connect_link(
                base_url,
                token=token,
                ntfy_server=route.ntfy_server or None,
                ntfy_topic=route.ntfy_topic or None,
                handle=user["handle"],
                display_name=user["display_name"] or None,
            )
            if args.rotate:
                print("Rotated the token (old one is now invalid).")
            print(f"Connect link for '{args.handle}':")
            print(f"  {link}")
            if not token:
                print(
                    "  (no token embedded — add --rotate to mint & embed one, "
                    "or have them paste their token in the app.)"
                )
            if args.qr:
                _print_qr(link)
            else:
                print("Add --qr to render a scannable QR for the setup sheet.")
    return 0


def _print_qr(data: str) -> None:
    """Render ``data`` as a terminal QR via the optional ``segno`` extra.

    QR rendering is opt-in (``pip install 'prefrontal[qr]'``) so the base install
    stays dependency-light; without it we point at the extra and leave the plain
    link (which is always printed) as the fallback.
    """
    try:
        import segno
    except ModuleNotFoundError:
        print(
            "  (install the QR extra to render a code here: "
            "pip install 'prefrontal[qr]' — or paste the link into any QR maker.)"
        )
        return
    print()
    segno.make(data, error="m").terminal(compact=True)


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
        elif args.household_action == "away":
            return _away_cli(store, args, settings)
        elif args.household_action == "shift":
            return _service_shift_cli(store, args, settings)
        elif args.household_action == "invite":
            from prefrontal.integrations.sms import normalize_phone, send_invite_sms

            scoped = _resolve_user_store(store, args.user)
            if scoped.household_id_or_none() is None:
                print("That user isn't in a household.", file=sys.stderr)
                return 1
            if args.sms and normalize_phone(args.sms) is None:
                print(
                    "--sms must be a phone number (E.164, e.g. '+14155551234').",
                    file=sys.stderr,
                )
                return 1
            inv = scoped.create_invite()
            base = settings.oauth_base_url
            join_url = f"{base}/household?invite={inv['code']}" if base else ""
            print(f"Invite code: {inv['code']}  (expires {inv['expires_at']} UTC)")
            fallback = join_url or f"<base-url>/household?invite={inv['code']}"
            print("Share it, or send: " + fallback)
            if args.sms:
                household = scoped.household()
                sms = send_invite_sms(
                    settings,
                    code=inv["code"],
                    join_url=join_url,
                    to=args.sms,
                    household_name=(household or {}).get("name"),
                )
                outcome = "sent" if sms.delivered else "not sent"
                print(f"SMS to {args.sms}: {outcome} ({sms.detail})")
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
    from prefrontal.clock import local_datetime
    from prefrontal.household import (
        describe_schedule,
        fmt_time_12h,
        normalize_chore,
        with_effective_schedule,
    )

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
                "month_days": args.month_days.split(",") if args.month_days else [],
                "owner_id": owner_id,
                "remind_before": args.remind,
                "impact": args.impact,
                "away_behavior": args.away_behavior or "keep",
                "service": args.service,
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
        from prefrontal.household import log_chore_done_and_celebrate

        today = local_datetime(utcnow(), settings.timezone).strftime("%Y-%m-%d")
        result = log_chore_done_and_celebrate(
            scoped, chore_id=args.done, done_on=today, done_by=scoped.user_id,
            settings=settings,
        )
        if result is None:
            print(f"No chore #{args.done}.", file=sys.stderr)
            return 1
        print(f"Marked #{args.done} done for today.")
        done = result.get("routine_completed")
        if done:
            print(f"🎉 That completes '{done['title']}' for today — both parents notified.")
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
        away = " [skipped while away]" if c.get("away_behavior") == "suppress" else ""
        svc = f" [service: {c['service']}]" if c.get("service") else ""
        impact = f" — {c['impact']}" if c.get("impact") else ""
        when = f"by {fmt_time_12h(eff['due_time'])}" if eff.get("due_time") else "untimed"
        routine = f" · {c['routine_title']}" if c.get("routine_title") else ""
        print(
            f"  [{box}] #{c['id']} {c['title']} "
            f"({owner} · {describe_schedule(eff['days'], eff.get('month_days'))} · {when}{routine})"
            f"{paused}{away}{svc}{impact}"
        )
    return 0


def _routines_cli(store, args, settings) -> int:
    """List the household's routines, or add/assign-accountable/pause/remove one.

    A routine groups chores under one **accountable** owner (the mental-load
    holder) and carries the schedule its chores inherit. ``--add`` defines one,
    ``--accountable`` sets who holds it, ``--remove`` deletes it (its chores
    survive, unlinked), and ``--enable``/``--disable`` pause or resume it.
    """
    from prefrontal.household import describe_schedule, fmt_time_12h, normalize_routine

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
                "month_days": args.month_days.split(",") if args.month_days else [],
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
            f"(accountable: {holder} · "
            f"{describe_schedule(r['days'], r.get('month_days'))} · {when} · "
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
        if s["stage"] == "suppressed":
            print(f"  → suppressed: {s['title']} ({s['reason']})")
        else:
            print(f"  → {s['stage']}: {s['title']} ({len(s['notified'])} notified)")
    return 0


def _away_cli(store, args, settings) -> int:
    """Show, set, or clear an away window — the household's, or (``--member``) one member's.

    Household away (the default) suppresses location-bound (``away_behavior=suppress``)
    chores for everyone; ``--member`` marks just this user away, so *their* chores
    fall to the present co-parent instead. ``--set START END`` sets it, ``--clear``
    removes it, no flags prints the current window.
    """
    from datetime import datetime

    from prefrontal.clock import local_datetime
    from prefrontal.household import away_covers
    from prefrontal.impact import utcnow

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1

    member = getattr(args, "member", False)
    who = "You're" if member else "Household"
    set_fn = scoped.set_member_away if member else scoped.set_away_window
    get_fn = scoped.member_away_window if member else scoped.away_window
    clear_fn = scoped.clear_member_away if member else scoped.clear_away_window

    if args.clear:
        clear_fn()
        print(f"Cleared the {'member' if member else 'household'} away window — back to present.")
        return 0
    if args.away_set:
        start, end = args.away_set
        for label, value in (("START", start), ("END", end)):
            try:
                datetime.strptime(value, "%Y-%m-%d")  # tz-ok: validates a local date
            except ValueError:
                print(f"{label} must be a 'YYYY-MM-DD' date.", file=sys.stderr)
                return 1
        if end < start:
            print("END must be on or after START.", file=sys.stderr)
            return 1
        set_fn(starts_on=start, ends_on=end, note=args.note)
        tail = f" ({args.note})" if args.note else ""
        print(f"{who} away {start} → {end}{tail}.")
        return 0

    window = get_fn()
    if window is None:
        scope = "--member " if member else ""
        print(f"Not away. Set a window: household away {scope}--set 2026-07-10 2026-07-17")
        return 0
    now_local = local_datetime(utcnow(), settings.timezone)
    active = "active now" if away_covers(window, now_local) else "not active today"
    note = f" ({window['note']})" if window.get("note") else ""
    print(f"Away {window['starts_on']} → {window['ends_on']}{note} — {active}.")
    return 0


def _service_shift_cli(store, args, settings) -> int:
    """Show, set, or clear a municipal service's holiday pickup-day shift for a week.

    ``--set SERVICE WEEKDAY`` records "SERVICE moved to WEEKDAY this week" (or the
    week containing ``--week``); ``--clear SERVICE`` removes it; no flags lists the
    stored shifts. This is the manual twin of the (deferred) weekly scrape — same
    store path — so a shift can be entered by hand today.
    """
    from prefrontal.clock import local_datetime
    from prefrontal.household import service_week
    from prefrontal.service_shifts import monday_of

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1

    labels = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def _week() -> str:
        if args.week:
            try:
                return monday_of(args.week)
            except ValueError:
                print("--week must be a 'YYYY-MM-DD' date.", file=sys.stderr)
                return ""
        return service_week(local_datetime(utcnow(), settings.timezone))

    if args.shift_clear:
        week = _week()
        if not week:
            return 1
        service = args.shift_clear[0].strip().lower()
        removed = scoped.clear_service_shift(service=service, week=week)
        print(f"Cleared {service} shift for week of {week}." if removed
              else f"No {service} shift stored for week of {week}.")
        return 0
    if args.shift_set:
        week = _week()
        if not week:
            return 1
        service = args.shift_set[0].strip().lower()
        raw_day = args.shift_set[1].strip()
        # Accept a weekday int (0=Mon) or a name/prefix like 'Wed'.
        weekday = None
        if raw_day.isdigit() and 0 <= int(raw_day) <= 6:
            weekday = int(raw_day)
        else:
            for i, name in enumerate(labels):
                if name.lower().startswith(raw_day[:3].lower()):
                    weekday = i
                    break
        if weekday is None:
            print("WEEKDAY must be 0–6 (0=Mon) or a name like 'Wed'.", file=sys.stderr)
            return 1
        scoped.set_service_shift(
            service=service, week=week, shifted_weekday=weekday, reason=args.reason
        )
        tail = f" ({args.reason})" if args.reason else ""
        print(f"Set {service} → {labels[weekday]} for week of {week}{tail}.")
        return 0

    shifts = scoped.service_shifts()
    if not shifts:
        print("No service shifts stored. Set one: "
              "household shift --set trash Wed --reason 'July 4th'")
        return 0
    for s in shifts:
        reason = f" ({s['reason']})" if s.get("reason") else ""
        print(f"  {s['service']}: week of {s['week']} → {labels[s['shifted_weekday'] % 7]}{reason}")
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
    from prefrontal.household import run_digest_sweep

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    result = run_digest_sweep(scoped, settings=settings)
    if result["reason"] == "not_shared":
        print("Single-parent household — the delta digest is off.")
        return 0
    if result["reason"] == "disabled":
        print("The daily digest is off for this household.")
        return 0
    for item in result["sent"]:
        row = item["delivery"]
        state = "sent" if row["delivered"] else "not sent"
        print(f"  → {item['handle']}: {item['count']} change(s) — {state} ({row['detail']})")
    if not result["sent"]:
        print("No digests to send (everyone's caught up or recently digested).")
    return 0


def _checkin_cli(store, args, settings) -> int:
    """Send the weekly mental-load check-in to both parents if it's due.

    The CLI twin of ``POST /webhooks/household/checkin/check`` — for a launchd
    trigger or a manual test. No-op (with a message) when the check-in is off or
    already sent this week.
    """
    from prefrontal.household import run_checkin_sweep

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    result = run_checkin_sweep(scoped, settings=settings)
    if result["reason"] == "not_shared":
        print("Single-parent household — the load check-in is off.")
        return 0
    if not result["sent"]:
        print("Weekly check-in not due right now (off, wrong day/time, or already sent).")
        return 0
    for row in result["notified"]:
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
    from prefrontal.household import run_star_prompt_sweep

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    result = run_star_prompt_sweep(scoped, settings=settings)
    for item in result["sent"]:
        print(f"Prompted: {item['title']} — “{item['question']}”")
    if not result["sent"]:
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
    from prefrontal.integrations.delivery import DeliveryClient, resolve_route
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
    :class:`~prefrontal.integrations.delivery.DeliveryClient` and per-user
    :func:`~prefrontal.integrations.delivery.resolve_route` the coaching tick uses
    — so it confirms ntfy/Pushover is wired up (server/topic/token or credentials)
    before you rely on a nudge landing. Prints where it routed and the transport's
    result; a plain push (no action buttons), so no signing config is needed.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``message``, ``channel``.

    Returns:
        ``0`` if a send succeeded, ``1`` if nothing was delivered (e.g. no
        transport configured, or the transport returned an error).
    """
    from prefrontal.coaching import Cue, Decision
    from prefrontal.integrations.delivery import DeliveryClient, resolve_route

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

    if route.ntfy_topic:
        dest = f"ntfy → {route.ntfy_server}/{route.ntfy_topic}"
    elif route.pushover_token and route.pushover_user_key:
        dest = "pushover"
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
                "\nNothing was sent — no transport is configured for this user. Set "
                "NTFY_TOPIC (and NTFY_SERVER/NTFY_TOKEN) or Pushover credentials in "
                "the environment, or a per-user ntfy_topic in coaching_state.",
                file=sys.stderr,
            )
        return 1
    return 0


def _cmd_cleanup_drops(args: argparse.Namespace) -> int:
    """Reclassify historical hygiene todo-drops from ``miss`` to ``discarded``.

    A one-off backfill for the fix in
    :func:`~prefrontal.todos.todo_episode_fields`: before it, *every* dropped todo
    logged a ``miss``, so quick "this is wrong" clears inflated the ``drift`` score
    and the briefing's "Slipped" count. This rescans the user's past todo-drop
    misses and downgrades the hygiene ones (dropped under the avoidance floor) to
    ``discarded`` — an aging drop is left a ``miss`` (it may be a genuine give-up).

    Dry-run by default (counts only); pass ``--apply`` to write. Idempotent — a
    re-run finds nothing left to change.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``apply``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        result = reclassify_hygiene_drops(store, apply=args.apply)

    verb = "reclassified" if args.apply else "would reclassify"
    print(
        f"scanned {result['scanned']} todo-drop miss episode(s); "
        f"{verb} {result['reclassified']} as hygiene drops."
    )
    for sample in result["samples"]:
        print(f"  - {sample}")
    if not args.apply and result["reclassified"]:
        print("\nDry run — re-run with --apply to write these changes.")
    return 0


def _cmd_cleanup_focus_estimates(args: argparse.Namespace) -> int:
    """Retract deliberately-switched focus blocks from the time-estimation signal.

    A one-off backfill for the fix in
    :func:`~prefrontal.modules.hyperfocus.record_focus_switched`: before it, a block
    you consciously switched away from logged its truncated duration as
    ``actual_value``, so it fed the ``time_estimation`` bias and dragged the
    multiplier toward zero (a cut-short block stopped by choice, not because the
    estimate was wrong). This nulls ``actual_value`` on those past episodes; the
    ``partial`` outcome is left intact for ``drift``.

    Dry-run by default (counts only); pass ``--apply`` to write. Idempotent. Run
    ``learn`` afterward to recompute the bias off the corrected history.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``apply``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.modules.hyperfocus import retract_switched_estimates

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        result = retract_switched_estimates(store, apply=args.apply)

    verb = "cleared" if args.apply else "would clear"
    print(
        f"scanned {result['scanned']} switched-focus episode(s); "
        f"{verb} {result['cleared']} from the estimation signal."
    )
    for sample in result["samples"]:
        print(f"  - {sample}")
    if not args.apply and result["cleared"]:
        print("\nDry run — re-run with --apply to write, then `learn` to recompute.")
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


def _cmd_clarify(args: argparse.Namespace) -> int:
    """Ambiguity clarifications: check / list / resolve / dismiss / guide.

    The CLI twin of the ``/clarifications`` surface (and the coaching-tick sweep).
    ``check`` runs the same :func:`~prefrontal.clarify.sweep_ambiguous_items` a tick
    does — filing an inline question for each newly-ambiguous todo/commitment
    (local model, heuristic fallback), never re-asking a known item. ``resolve``
    records the chosen reading (and prints the guided playbook when it maps to a
    recognized task); ``dismiss`` marks an item not-ambiguous; ``guide`` previews a
    playbook's steps by task type (static content, needs no store).

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``clarify_action`` and
            the per-action fields (``limit``/``status``/``id``/``option``/
            ``answer``/``task_type``).

    Returns:
        Process exit code (0 on success, 1 on a bad id / unknown task type / input).
    """
    from prefrontal.clarify import (
        HOME_ZIP_KEY,
        LOCALIZATION_KEY,
        apply_clarification_answer,
        known_task_types,
        localized_zip,
        playbook_view,
        resolve_playbook,
        sweep_ambiguous_items,
    )
    from prefrontal.integrations.ollama import OllamaClient

    def _show_playbook(pb, zip_code) -> None:
        # Render through playbook_view so the same {area} localization the HTTP/
        # dashboard surfaces use applies here (zip_code is None when opted out).
        view = playbook_view(pb, zip_code=zip_code)
        print(f"Guide: {view['title']}")
        if view["intro"]:
            print(f"  {view['intro']}")
        for i, step in enumerate(view["steps"], 1):
            print(f"  {i}. {step['title']}")
            if step["detail"]:
                print(f"     {step['detail']}")

    def _show(row) -> None:
        mark = {"pending": "?", "resolved": "✓", "dismissed": "—"}.get(row["status"], "?")
        print(f"#{row['id']} [{mark}] {row['title']} ({row['target_type']}) — {row['question']}")
        if row["status"] == "pending":
            known = known_task_types()
            for i, opt in enumerate(row.get("options") or []):
                guide = "  ▸ has guide" if opt.get("task_type") in known else ""
                print(f"    [{i}] {opt.get('label')}{guide}")
        elif row.get("answer"):
            tail = f"  → guide: {row['task_type']}" if row.get("task_type") else ""
            print(f"    answered: {row['answer']}{tail}")

    settings = get_settings()
    action = args.clarify_action

    with MemoryStore.open(args.db_path or settings.db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        zip_code = localized_zip(store)  # None unless the user opted into localization
        if action == "localize":
            # Opt in/out of ZIP-localized guides, optionally setting the home ZIP.
            if args.zip is not None:
                store.set_state(HOME_ZIP_KEY, args.zip.strip(), source="explicit")
            store.set_state(
                LOCALIZATION_KEY, "1" if args.state == "on" else "0", source="explicit"
            )
            zip_now = (store.get_state(HOME_ZIP_KEY) or "").strip() or "(unset)"
            if args.state == "on":
                print(f"Playbook localization ON — guides will use ZIP {zip_now}.")
            else:
                print("Playbook localization OFF — guides use generic phrasing.")
            return 0
        if action == "guide":
            playbook = resolve_playbook(args.task_type)
            if playbook is None:
                known = ", ".join(sorted(known_task_types()))
                print(
                    f"No playbook for task type {args.task_type!r}. Known: {known}",
                    file=sys.stderr,
                )
                return 1
            _show_playbook(playbook, zip_code)
            return 0
        if action == "check":
            client = OllamaClient(
                base_url=settings.ollama_url, model=settings.ollama_model
            )
            ids = sweep_ambiguous_items(store, client, limit=args.limit)
            if not ids:
                print("No new ambiguous items found.")
                return 0
            print(f"Flagged {len(ids)} item(s) for clarification:")
            for cid in ids:
                row = store.get_clarification(cid)
                if row is not None:
                    _show(row)
            return 0
        if action == "list":
            rows = store.list_clarifications(args.status)
            if not rows:
                print(f"No {args.status} clarifications.")
                return 0
            for row in rows:
                _show(row)
            return 0
        if action == "dismiss":
            if not store.dismiss_clarification(args.id):
                print(f"No pending clarification #{args.id}.", file=sys.stderr)
                return 1
            print(f"Dismissed #{args.id} — marked not ambiguous.")
            return 0
        # resolve
        row = store.get_clarification(args.id)
        if row is None or row["status"] != "pending":
            print(f"No pending clarification #{args.id}.", file=sys.stderr)
            return 1
        try:
            result = apply_clarification_answer(
                store, row, option_index=args.option, answer=args.answer
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Resolved #{args.id}: {result['answer']}")
        if result["playbook"] is not None:
            print()
            _show_playbook(result["playbook"], zip_code)
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
                started = " ▶ started" if t.get("started_at") else ""
                print(f"#{t['id']} [P{t['priority']}]{est}{cat} {t['title']}{started}")
        elif args.todo_action == "start":
            if store.start_todo(args.todo_id):
                print(f"Todo #{args.todo_id} marked started. 💪")
            else:
                t = store.get_todo(args.todo_id)
                if t is not None and t.get("status") == "open":
                    print(f"Todo #{args.todo_id} was already started.")
                else:
                    print(f"Todo #{args.todo_id} is not open.", file=sys.stderr)
                    return 1
        elif args.todo_action == "unstart":
            if store.unstart_todo(args.todo_id):
                print(f"Todo #{args.todo_id} no longer marked started.")
            else:
                print(f"Todo #{args.todo_id} wasn't marked started.", file=sys.stderr)
                return 1
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
        elif args.todo_action == "delegate":
            from prefrontal.delegation import run_delegation
            from prefrontal.integrations.ollama import OllamaClient
            from prefrontal.sources import resolve_smtp

            todo = store.get_todo(args.todo_id)
            if todo is None or todo.get("status") != "open":
                print(f"Todo #{args.todo_id} is not open.", file=sys.stderr)
                return 1
            destination = (args.to or "").strip() or None
            if args.handler == "email" and destination is None:
                print("`--to <email>` is required for --handler email.", file=sys.stderr)
                return 1
            # Use the local model when it's up; otherwise prep falls back to a
            # heuristic brief (client=None) rather than blocking on a down model.
            ollama = OllamaClient(
                base_url=settings.ollama_url, model=settings.ollama_model
            )
            client = ollama if ollama.available() else None
            smtp = resolve_smtp(store) if args.handler == "email" else None
            result = run_delegation(
                store,
                todo,
                handler=args.handler,
                destination=destination,
                context=(args.context or "").strip() or None,
                va_note=(args.note or "").strip() or None,
                client=client,
                smtp=smtp,
            )
            print(f"Todo #{args.todo_id} delegated to {result.handler} → {result.status}.")
            if result.detail:
                print(f"  {result.detail}")
            if result.brief:
                print(f"  Brief: {result.brief.splitlines()[0]}")
            if result.drafts:
                print(f"  {len(result.drafts)} draft(s) prepared.")
            # A failed email hand-off still stored the brief — surface that it's not lost.
            if result.status == "failed":
                return 1
    return 0


def _cmd_fit(args: argparse.Namespace) -> int:
    """Show open todos that fit a block of free time, applying the time bias.

    Args:
        args: Parsed arguments; uses ``minutes`` and ``db_path``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.clock import local_datetime
    from prefrontal.memory.patterns import task_bias_resolver

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)

        # "right now" → calibrate each todo with this hour's band, its energy /
        # category, then the task-type bias, else global (§5).
        now_hour = local_datetime(utcnow(), settings.timezone).hour
        fits = fit_todos(
            args.minutes, store.open_todos(exclude_delegated=True, with_project_rank=True),
            bias_fn=task_bias_resolver(store, local_hour=now_hour),
        )
    print(f"With {args.minutes:g} minutes free, you could knock out:")
    if not fits:
        print("  (nothing with an estimate fits — add estimates with `todo add --minutes`)")
    for f in fits:
        t = f["todo"]
        print(f"  #{t['id']} {t['title']} (~{f['effective_minutes']:g}m)")
    return 0


def _ingest_mail(store, messages, *, account, policy, client, use_model, settings):
    """Ingest a batch of messages for one account into a (scoped) store.

    Centralizes the ingest call — learned corrections/denylist, per-account
    domain — so the ``sync`` and (per-user, per-account) ``fetch`` paths share it.
    """
    from prefrontal.mail import ingest_messages
    from prefrontal.mail.feedback import learned_corrections, learned_denylist

    return ingest_messages(
        store,
        messages,
        account=account,
        policy=policy,
        client=client,
        use_model=use_model,
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


def _print_mail_summary(summary, *, handle: str | None = None) -> None:
    """Print one ingest-summary line, optionally prefixed with the user handle."""
    prefix = f"[{handle}] " if handle else ""
    print(
        f"{prefix}[{summary.account}/{summary.policy}] received {summary.received}, "
        f"ingested {summary.ingested}, skipped {summary.skipped}, "
        f"needs-action {summary.needs_action} "
        f"({summary.todos_created} todos, {summary.todos_suppressed} suppressed), "
        f"{summary.triaged_by_llm} via model."
    )


def _cmd_calendar_sources(
    args: argparse.Namespace, settings, db_path: str
) -> int:
    """Manage a user's ICS calendar feeds: add / list / remove.

    ``add-source`` seals the feed URL at rest (so it needs `prefrontal secrets
    init` first); ``list-sources`` never prints the URL (it's a bearer secret).
    """
    from prefrontal.crypto import SecretKeyError, secret_key_configured
    from prefrontal.sources import ICS, ics_sources, put_ics_source

    action = args.calendar_action
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)

        if action == "list-sources":
            rows = ics_sources(store, include_disabled=True)
            if not rows:
                print("No calendar (ICS) sources configured for this user.")
                return 0
            for s in rows:
                state = "enabled" if s.enabled else "disabled"
                me = f" me={','.join(s.me_emails)}" if s.me_emails else ""
                label = f" ({s.label})" if s.label else ""
                # The feed URL is a bearer secret — never printed.
                print(f"{s.account}{label}: namespace={s.namespace} {state}{me}")
            return 0

        if action == "remove-source":
            if store.delete_source(ICS, args.account):
                print(f"Removed calendar source '{args.account}'.")
                return 0
            print(f"No calendar source '{args.account}' for this user.", file=sys.stderr)
            return 1

        # add-source seals the URL — require a key.
        if not secret_key_configured(settings):
            print(
                "No secret key configured — run `prefrontal secrets init` first "
                "(the feed URL is encrypted at rest).",
                file=sys.stderr,
            )
            return 1
        me_emails = tuple(e.strip() for e in (args.me or "").split(",") if e.strip())
        try:
            put_ics_source(
                store,
                account=args.account,
                url=args.url,
                namespace=args.namespace,
                me_emails=me_emails,
                label=args.label,
                enabled=not args.disabled,
            )
        except SecretKeyError as exc:
            print(f"Could not seal the feed URL: {exc}", file=sys.stderr)
            return 1
        state = "disabled" if args.disabled else "enabled"
        print(f"Saved calendar source '{args.account}' ({state}).")
        return 0


def _cmd_calendar(args: argparse.Namespace) -> int:
    """Manage ICS calendar feeds and sync them into ``commitments``.

    - ``add-source`` / ``list-sources`` / ``remove-source`` — manage a user's
      private ICS feeds (URLs sealed at rest).
    - ``sync`` — fetch + parse each feed and upsert its events (the native,
      no-n8n calendar path). ``--all-users`` fans out over every active user;
      each user's own feeds land only in their own scope.

    Args:
        args: Parsed arguments; ``calendar_action`` plus action-specific fields.

    Returns:
        Process exit code (0 on success; 1 if any feed/user failed).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path

    if args.calendar_action in ("add-source", "list-sources", "remove-source"):
        return _cmd_calendar_sources(args, settings, db_path)

    # sync
    from prefrontal.classify import classify_kind
    from prefrontal.commitments import sync_calendar
    from prefrontal.geocode import enrich_commitments
    from prefrontal.ics import fetch_ics, parse_ics
    from prefrontal.integrations.nominatim import NominatimGeocoder
    from prefrontal.integrations.ollama import OllamaClient
    from prefrontal.sources import ics_sources

    ollama = OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)
    # One liveness check up front: when Ollama is down, new events default to
    # 'self' (the conservative, conflict-preserving choice) rather than a slow
    # per-title timeout storm.
    ollama_up = ollama.available()

    status = 0
    with MemoryStore.open(db_path) as unscoped:
        for handle, store in _user_targets(unscoped, args):
            feeds = ics_sources(store, include_disabled=False)
            if not feeds:
                print(f"[{handle}] no calendar sources configured.", file=sys.stderr)
                continue
            events: list[dict] = []
            for feed in feeds:
                try:
                    text = fetch_ics(feed.url)
                except Exception as exc:  # httpx raises a variety of errors
                    print(
                        f"[{handle}/{feed.account}] ICS fetch failed: {exc}",
                        file=sys.stderr,
                    )
                    status = 1
                    continue
                events.extend(
                    parse_ics(text, namespace=feed.namespace, me_emails=feed.me_emails)
                )

            # The roster pass is deterministic/offline, so a kid's appointment
            # still lands on the shared sheet as 'child' even when Ollama is down;
            # build the classifier whenever either signal is available.
            child_names = store.child_names()
            examples = store.kind_feedback_examples() if ollama_up else None
            classify = None
            if ollama_up or child_names:

                def classify(title, _c=ollama if ollama_up else None,
                             _ex=examples, _names=child_names):
                    return classify_kind(title, client=_c, examples=_ex, child_names=_names)

            try:
                summary = sync_calendar(
                    store, events, classify=classify, default_tz=settings.timezone,
                    recur_horizon_hours=settings.calendar_horizon_days * 24.0,
                )
            except ValueError as exc:
                print(f"[{handle}] calendar sync rejected: {exc}", file=sys.stderr)
                status = 1
                continue

            # Best-effort geocode: curated places + cache always; the network
            # geocoder only when this user turned geocoding on.
            geocoder = (
                NominatimGeocoder.from_settings(settings)
                if store.get_bool("geocoding_enabled", False)
                else None
            )
            geo = enrich_commitments(store, geocoder=geocoder)
            skipped = (
                f" — skipped {summary.skipped} invalid "
                f"({', '.join(summary.skipped_titles[:5])})"
                if summary.skipped else ""
            )
            print(
                f"[{handle}] calendar: +{summary.added} ~{summary.updated} "
                f"-{summary.cancelled} cancelled, {summary.upcoming} upcoming, "
                f"{summary.conflicts} conflict(s) (geocoded {geo['resolved']}){skipped}."
            )
    return status


def _cmd_mail_sources(
    args: argparse.Namespace, settings, db_path: str
) -> int:
    """Manage a user's per-user IMAP sources: add / list / remove / import-env.

    ``add-source`` and ``import-env-sources`` seal the password with the at-rest
    key (so they require `prefrontal secrets init` first); ``list-sources`` never
    reveals secrets; ``import-env-sources`` migrates the legacy global
    ``MAIL_IMAP_*`` env accounts into the resolved user's registry.
    """
    import json

    from prefrontal.crypto import SecretKeyError, secret_key_configured
    from prefrontal.mail.imap import DEFAULT_IMAP_HOST, ImapAccount
    from prefrontal.sources import IMAP, put_imap_source

    action = args.mail_action
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)

        if action == "list-sources":
            rows = store.list_sources(kind=IMAP)
            if not rows:
                print("No IMAP sources configured for this user.")
                return 0
            for r in rows:
                cfg = json.loads(r["config"] or "{}")
                state = "enabled" if r["enabled"] else "disabled"
                imp = " important-only" if cfg.get("important_only") else ""
                print(
                    f"{r['account']}: {cfg.get('username', '?')}@{cfg.get('host', '?')} "
                    f"[{cfg.get('mailbox', 'INBOX')}] {cfg.get('retention', 'signals')} "
                    f"{state}{imp}"
                )
            return 0

        if action == "remove-source":
            if store.delete_source(IMAP, args.account):
                print(f"Removed IMAP source '{args.account}'.")
                return 0
            print(f"No IMAP source '{args.account}' for this user.", file=sys.stderr)
            return 1

        # add-source / import-env-sources both seal secrets — require a key.
        if not secret_key_configured(settings):
            print(
                "No secret key configured — run `prefrontal secrets init` first "
                "(source secrets are encrypted at rest).",
                file=sys.stderr,
            )
            return 1

        if action == "add-source":
            host = args.host or DEFAULT_IMAP_HOST
            password = args.password
            if password is None:
                import getpass

                try:
                    password = getpass.getpass(f"IMAP password for {args.username}: ")
                except (EOFError, KeyboardInterrupt):
                    print("\nAborted.", file=sys.stderr)
                    return 1
            if not password:
                print("A password is required.", file=sys.stderr)
                return 1
            important = args.important_only
            if important is None:  # default: Important-only for Gmail hosts
                important = "gmail" in host.lower()
            try:
                put_imap_source(
                    store,
                    account=args.account,
                    host=host,
                    username=args.username,
                    password=password,
                    mailbox=args.mailbox,
                    important_only=important,
                    retention=args.retention,
                    enabled=not args.disabled,
                )
            except SecretKeyError as exc:
                print(f"Could not seal the password: {exc}", file=sys.stderr)
                return 1
            state = "disabled" if args.disabled else "enabled"
            print(f"Saved IMAP source '{args.account}' ({state}).")
            return 0

        # import-env-sources
        names = [name for name, _ in settings.mail_accounts]
        if not names:
            print("No accounts in PREFRONTAL_MAIL_ACCOUNTS to import.", file=sys.stderr)
            return 1
        imported: list[str] = []
        skipped: list[str] = []
        for account in names:
            imap = ImapAccount.from_env(account)
            if imap is None:
                skipped.append(account)
                continue
            put_imap_source(
                store,
                account=account,
                host=imap.host,
                username=imap.user,
                password=imap.password,
                mailbox=imap.mailbox,
                important_only=imap.important_only,
                retention=settings.policy_for(account),
            )
            imported.append(account)
        if imported:
            print(f"Imported {len(imported)} source(s): {', '.join(imported)}")
        if skipped:
            print(f"Skipped (no MAIL_IMAP_* creds): {', '.join(skipped)}")
        return 0 if imported else 1


def _cmd_mail(args: argparse.Namespace) -> int:
    """Ingest, fetch, or list triaged mail.

    Subcommands:

    - ``list`` — show recently ingested mail and the open action items.
    - ``sync FILE`` — ingest messages from a JSON file (a list, or an object
      with a ``messages`` key) for the given ``--account``.
    - ``fetch`` — pull unread mail over IMAP, then ingest. Credentials +
      retention resolve per user from the registry (``add-source``), falling back
      to the global ``MAIL_IMAP_*_<ACCOUNT>`` env. ``--account`` fetches one
      account; omit it to fetch all of the user's accounts. ``--all-users`` fans
      out over every active user.
    - ``add-source`` / ``list-sources`` / ``remove-source`` — manage a user's
      connected IMAP mailboxes (passwords sealed at rest).
    - ``import-env-sources`` — migrate the legacy global ``MAIL_IMAP_*`` accounts
      into the user's registry.

    Args:
        args: Parsed arguments; ``mail_action`` plus action-specific fields.

    Returns:
        Process exit code (0 on success, 1 on a usage/credential error).
    """
    import json

    from prefrontal.integrations.ollama import OllamaClient
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

    if args.mail_action in ("add-source", "list-sources", "remove-source",
                            "import-env-sources"):
        return _cmd_mail_sources(args, settings, db_path)

    client = OllamaClient(base_url=settings.ollama_url, model=settings.ollama_model)

    if args.mail_action == "sync":
        account = args.account
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
        with MemoryStore.open(db_path) as unscoped:
            store = _resolve_user_store(unscoped, args.user)
            summary = _ingest_mail(
                store,
                messages,
                account=account,
                policy=settings.policy_for(account),
                client=client,
                use_model=not args.heuristic,
                settings=settings,
            )
        _print_mail_summary(summary)
        return 0

    # fetch: pull unread over IMAP for each target user's accounts, then ingest.
    # Credentials + retention come from each user's registry sources, falling back
    # to the global MAIL_IMAP_* env so a not-yet-migrated single-user deploy still
    # works. Under --all-users the env fallback is OFF: that global config is one
    # mailbox, so a source-less user is skipped rather than fetching it into their
    # scope (the multi-tenant leak).
    from prefrontal.mail.imap import fetch_unread
    from prefrontal.sources import mail_fetch_accounts, resolve_mail_fetch

    allow_env = not args.all_users
    status = 0
    with MemoryStore.open(db_path) as unscoped:
        for handle, store in _user_targets(unscoped, args):
            accounts = (
                [args.account]
                if args.account
                else mail_fetch_accounts(
                    store, settings=settings, allow_env_fallback=allow_env
                )
            )
            if not accounts:
                print(f"[{handle}] no mail accounts configured.", file=sys.stderr)
                continue
            for account in accounts:
                src = resolve_mail_fetch(
                    store, account, settings=settings, allow_env_fallback=allow_env
                )
                if src is None:
                    print(
                        f"[{handle}/{account}] no IMAP credentials — add a source "
                        f"(`prefrontal mail --user {handle} add-source --account "
                        f"{account} ...`) or set MAIL_IMAP_*_{account.upper()}.",
                        file=sys.stderr,
                    )
                    status = 1
                    continue
                try:
                    messages = fetch_unread(
                        src.imap, limit=args.limit, since_days=args.since_days
                    )
                except Exception as exc:  # imaplib raises a variety of errors
                    print(
                        f"[{handle}/{account}] IMAP fetch failed: {exc}",
                        file=sys.stderr,
                    )
                    status = 1
                    continue
                summary = _ingest_mail(
                    store,
                    messages,
                    account=account,
                    policy=src.policy,
                    client=client,
                    use_model=not args.heuristic,
                    settings=settings,
                )
                _print_mail_summary(summary, handle=handle)
    return status


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

    p_secrets = sub.add_parser(
        "secrets", help="Manage the at-rest secret key for source credentials."
    )
    secrets_sub = p_secrets.add_subparsers(dest="secrets_action", required=True)
    secrets_sub.add_parser(
        "init", help="Generate a Fernet secret key if none is configured."
    )
    secrets_sub.add_parser(
        "status", help="Report whether a usable secret key is configured."
    )
    p_secrets.set_defaults(func=_cmd_secrets)

    p_user = sub.add_parser(
        "user", help="Provision users (add/list/rotate/disable/route)."
    )
    p_user.add_argument("--db-path", default=None, help="Override the database path.")
    user_sub = p_user.add_subparsers(dest="user_action", required=True)
    u_add = user_sub.add_parser("add", help="Provision a new user (prints a token).")
    u_add.add_argument("handle", help="Unique short handle, e.g. 'sam'.")
    u_add.add_argument("--display-name", default=None, help="Name shown in nudges.")
    u_add.add_argument(
        "--operator", action="store_true", help="Grant admin (operator) rights."
    )
    u_add.add_argument(
        "--email",
        default=None,
        help="Verified Google email that signs this user in (for browser login).",
    )
    user_sub.add_parser("list", help="List users (never their tokens).")
    u_email = user_sub.add_parser(
        "email", help="Set/clear a user's Google sign-in email."
    )
    u_email.add_argument("handle", help="The user's handle.")
    u_email.add_argument(
        "email", nargs="?", default="", help="Google email (omit/pass '' to clear)."
    )
    u_rotate = user_sub.add_parser("rotate", help="Mint a new token for a user.")
    u_rotate.add_argument("handle", help="The user's handle.")
    u_disable = user_sub.add_parser("disable", help="Disable a user's access.")
    u_disable.add_argument("handle", help="The user's handle.")
    u_route = user_sub.add_parser(
        "route",
        help="Set/show a user's per-user delivery route (their own ntfy topic, etc.).",
    )
    u_route.add_argument("handle", help="The user's handle.")
    u_route.add_argument(
        "--ntfy-topic",
        default=None,
        help="Their own ntfy topic, so nudges hit THEIR phone (pass '' to clear).",
    )
    u_route.add_argument(
        "--ntfy-server", default=None, help="Override the ntfy server (pass '' to clear)."
    )
    u_route.add_argument(
        "--ntfy-token",
        default=None,
        help="ntfy access token for a protected topic (pass '' to clear).",
    )
    u_route.add_argument(
        "--pushover-user-key", default=None, help="Pushover user key (pass '' to clear)."
    )
    u_route.add_argument(
        "--pushover-token", default=None, help="Pushover app token (pass '' to clear)."
    )
    u_link = user_sub.add_parser(
        "connect-link",
        help="Print a prefrontal://connect deep link (+QR) to onboard their phone.",
    )
    u_link.add_argument("handle", help="The user's handle.")
    u_link.add_argument(
        "--rotate",
        action="store_true",
        help="Mint a fresh token and embed it (invalidates the old one).",
    )
    u_link.add_argument(
        "--qr", action="store_true", help="Also render a scannable QR (needs the 'qr' extra)."
    )
    u_link.add_argument(
        "--base-url",
        default=None,
        help="Override the deployment origin (defaults to OAUTH_BASE_URL).",
    )
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
    h_chore.add_argument(
        "--month-days",
        dest="month_days",
        default=None,
        help="Day-of-month CSV '1,15' (1–31; when set, wins over --days).",
    )
    h_chore.add_argument("--owner", default=None, help="Owner's handle (with --add).")
    h_chore.add_argument(
        "--routine", default=None, help="Routine title to file this chore under (with --add)."
    )
    h_chore.add_argument(
        "--remind", type=int, default=30, help="Minutes before due to nudge (with --add)."
    )
    h_chore.add_argument("--impact", default=None, help="Why it matters if it slips.")
    h_chore.add_argument(
        "--away-behavior",
        dest="away_behavior",
        choices=("keep", "suppress"),
        default=None,
        help="While the household is away: 'keep' (default) or 'suppress' "
        "(location-bound — trash/mail; skipped on vacation).",
    )
    h_chore.add_argument(
        "--service",
        default=None,
        help="Link to a municipal service (e.g. 'trash') whose pickup day can shift "
        "on a holiday week — the reminder then follows the shift (see 'household shift').",
    )
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
    h_routine.add_argument(
        "--month-days",
        dest="month_days",
        default=None,
        help="Day-of-month CSV '1,15' (1–31; when set, wins over --days).",
    )
    h_routine.add_argument("--impact", default=None, help="Why the routine matters if it slips.")
    h_routine.add_argument("--remove", type=int, default=None, help="Remove routine by id.")
    h_routine.add_argument("--enable", type=int, default=None, help="Resume routine by id.")
    h_routine.add_argument("--disable", type=int, default=None, help="Pause routine by id.")
    h_chk = house_sub.add_parser(
        "chores-check", help="Fire any chore reminders / miss-handoffs due now."
    )
    h_chk.add_argument("--user", default=None, help="Handle of a household member.")
    h_away = house_sub.add_parser(
        "away",
        help="Show/set/clear the 'we're away' window (skips away_behavior=suppress chores).",
    )
    h_away.add_argument("--user", default=None, help="Handle of a household member.")
    h_away.add_argument(
        "--member",
        action="store_true",
        help="Operate on just this user's away status (their chores fall to the "
        "co-parent) instead of the whole household.",
    )
    h_away.add_argument(
        "--set",
        dest="away_set",
        nargs=2,
        metavar=("START", "END"),
        default=None,
        help="Set the away window: two inclusive local dates 'YYYY-MM-DD YYYY-MM-DD'.",
    )
    h_away.add_argument(
        "--note", default=None, help="Short reason (with --set), e.g. 'beach trip'."
    )
    h_away.add_argument(
        "--clear", action="store_true", help="Clear the away window (back to not-away)."
    )
    h_shift = house_sub.add_parser(
        "shift",
        help="Show/set/clear a municipal service's holiday pickup-day shift for a week.",
    )
    h_shift.add_argument("--user", default=None, help="Handle of a household member.")
    h_shift.add_argument(
        "--set",
        dest="shift_set",
        nargs=2,
        metavar=("SERVICE", "WEEKDAY"),
        default=None,
        help="Set a shift: SERVICE (e.g. trash) and the WEEKDAY it moved to "
        "(0=Mon…6=Sun, or a name like 'Wed').",
    )
    h_shift.add_argument(
        "--week",
        default=None,
        help="The affected week as any date in it 'YYYY-MM-DD' (default: this week).",
    )
    h_shift.add_argument("--reason", default=None, help="Why it shifted, e.g. 'July 4th'.")
    h_shift.add_argument(
        "--clear",
        dest="shift_clear",
        nargs=1,
        metavar="SERVICE",
        default=None,
        help="Clear the shift for SERVICE in --week (default: this week).",
    )
    h_inv = house_sub.add_parser(
        "invite", help="Generate a shareable invite code for your household."
    )
    h_inv.add_argument("--user", default=None, help="Handle of a household member.")
    h_inv.add_argument(
        "--sms",
        default=None,
        help="Text the invite link to this phone number via Twilio (E.164, e.g. '+14155551234').",
    )
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

    p_notify = sub.add_parser(
        "notify",
        help="Send a test notification through the configured route (ntfy/Pushover).",
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

    p_cleanup = sub.add_parser(
        "cleanup-drops",
        help="Reclassify past hygiene todo-drops from 'miss' to 'discarded' (one-off).",
    )
    p_cleanup.add_argument("--db-path", default=None, help="Override the database path.")
    p_cleanup.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_cleanup.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes (default is a dry run that only reports counts).",
    )
    p_cleanup.set_defaults(func=_cmd_cleanup_drops)

    p_cfe = sub.add_parser(
        "cleanup-focus-estimates",
        help="Retract deliberately-switched focus blocks from the estimation bias (one-off).",
    )
    p_cfe.add_argument("--db-path", default=None, help="Override the database path.")
    p_cfe.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_cfe.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes (default is a dry run that only reports counts).",
    )
    p_cfe.set_defaults(func=_cmd_cleanup_focus_estimates)

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

    p_clarify = sub.add_parser(
        "clarify",
        help="Ambiguity clarifications: check / list / resolve / dismiss / guide.",
    )
    p_clarify.add_argument("--db-path", default=None, help="Override the database path.")
    p_clarify.add_argument("--user", default=None, help="Handle of the user to act on.")
    cl_sub = p_clarify.add_subparsers(dest="clarify_action", required=True)
    cl_check = cl_sub.add_parser(
        "check", help="Detect ambiguous items and file inline questions (the tick sweep)."
    )
    cl_check.add_argument(
        "--limit",
        type=int,
        default=MAX_SWEEP_ITEMS,
        help=f"Max items inspected this run (default {MAX_SWEEP_ITEMS}).",
    )
    cl_list = cl_sub.add_parser("list", help="List clarifications by status.")
    cl_list.add_argument(
        "--status",
        default="pending",
        choices=["pending", "resolved", "dismissed"],
        help="Which to list (default pending).",
    )
    cl_resolve = cl_sub.add_parser("resolve", help="Answer a pending clarification by id.")
    cl_resolve.add_argument("id", type=int, help="Clarification id.")
    cl_resolve.add_argument(
        "--option", type=int, default=None, help="Index of a candidate reading to pick."
    )
    cl_resolve.add_argument(
        "--answer", default=None, help="Free-text reading (when not picking an --option)."
    )
    cl_dismiss = cl_sub.add_parser("dismiss", help="Mark a pending clarification not-ambiguous.")
    cl_dismiss.add_argument("id", type=int, help="Clarification id.")
    cl_guide = cl_sub.add_parser("guide", help="Print a task type's guided playbook.")
    cl_guide.add_argument("task_type", help="e.g. tax_filing.")
    cl_localize = cl_sub.add_parser(
        "localize", help="Opt in/out of ZIP-localized guides (opt-in; off by default)."
    )
    cl_localize.add_argument("state", choices=["on", "off"], help="Turn localization on or off.")
    cl_localize.add_argument(
        "--zip", default=None, help="Set the home ZIP used to localize guides."
    )
    p_clarify.set_defaults(func=_cmd_clarify)

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
    t_start = todo_sub.add_parser("start", help="Mark that you've started a todo.")
    t_start.add_argument("todo_id", type=int)
    t_unstart = todo_sub.add_parser("unstart", help="Undo a mistaken 'started'.")
    t_unstart.add_argument("todo_id", type=int)
    t_done = todo_sub.add_parser("done", help="Mark a todo done.")
    t_done.add_argument("todo_id", type=int)
    t_drop = todo_sub.add_parser("drop", help="Drop a todo.")
    t_drop.add_argument("todo_id", type=int)
    t_domain = todo_sub.add_parser("domain", help="Set/clear a todo's work/home domain.")
    t_domain.add_argument("todo_id", type=int)
    t_domain.add_argument(
        "domain", nargs="?", default=None, help="work / home / … (omit to clear)."
    )
    t_delegate = todo_sub.add_parser(
        "delegate", help="Hand a todo to an assistant to do the prep / follow-up."
    )
    t_delegate.add_argument("todo_id", type=int)
    t_delegate.add_argument(
        "--handler", choices=["agent", "email"], default="agent",
        help="agent = in-app AI prep (default); email = send the brief to a human VA.",
    )
    t_delegate.add_argument(
        "--to", default=None, help="The assistant's email address (required for --handler email)."
    )
    t_delegate.add_argument(
        "--context", default=None,
        help="Optional free-text context to feed the prep (e.g. pasted work-AI output).",
    )
    t_delegate.add_argument(
        "--note", default=None,
        help="Optional cover note shown atop the email to a human VA (--handler email).",
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
    m_fetch.add_argument(
        "--account",
        default=None,
        help="Logical account name; omit to fetch all of the user's accounts.",
    )
    m_fetch.add_argument(
        "--all-users",
        action="store_true",
        help="Fan out over every active user (each user's own accounts).",
    )
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

    m_add_src = mail_sub.add_parser(
        "add-source", help="Add/update a per-user IMAP source (credentials sealed)."
    )
    m_add_src.add_argument("--account", required=True, help="Logical account name.")
    m_add_src.add_argument("--host", default=None, help="IMAP host (default imap.gmail.com).")
    m_add_src.add_argument("--username", required=True, help="IMAP login (usually the email).")
    m_add_src.add_argument(
        "--password",
        default=None,
        help="IMAP password/app-password. Omit to be prompted (not echoed).",
    )
    m_add_src.add_argument("--mailbox", default="INBOX", help="Mailbox to read (default INBOX).")
    m_add_src.add_argument(
        "--important-only",
        dest="important_only",
        action="store_true",
        default=None,
        help="Gmail: fetch only Important mail (default: on for Gmail hosts).",
    )
    m_add_src.add_argument(
        "--no-important-only",
        dest="important_only",
        action="store_false",
        help="Fetch all unread, not just Gmail-Important.",
    )
    m_add_src.add_argument(
        "--retention",
        choices=("signals", "full"),
        default="signals",
        help="Retention policy for this account (default signals).",
    )
    m_add_src.add_argument(
        "--disabled", action="store_true", help="Add the source paused (enabled=0)."
    )
    mail_sub.add_parser("list-sources", help="List the user's IMAP sources (no secrets).")
    m_rm_src = mail_sub.add_parser("remove-source", help="Delete a per-user IMAP source.")
    m_rm_src.add_argument("--account", required=True, help="Logical account name.")
    mail_sub.add_parser(
        "import-env-sources",
        help="Seal the current MAIL_IMAP_* env accounts into the user's registry.",
    )
    p_mail.set_defaults(func=_cmd_mail)

    p_cal = sub.add_parser("calendar", help="Manage private ICS calendar feeds + sync.")
    p_cal.add_argument("--db-path", default=None, help="Override the database path.")
    p_cal.add_argument("--user", default=None, help="Handle of the user to act on.")
    cal_sub = p_cal.add_subparsers(dest="calendar_action", required=True)
    c_add = cal_sub.add_parser(
        "add-source", help="Add/update a private ICS feed (URL sealed at rest)."
    )
    c_add.add_argument(
        "--account", required=True, help="Feed slug (also the calendar namespace)."
    )
    c_add.add_argument(
        "--url", required=True, help="Private ICS feed URL (the secret iCal address)."
    )
    c_add.add_argument(
        "--namespace", default=None, help="external_id namespace (default: the slug)."
    )
    c_add.add_argument(
        "--me",
        default=None,
        help="Comma-separated your-own emails; events you've declined are dropped.",
    )
    c_add.add_argument("--label", default=None, help="Optional display label.")
    c_add.add_argument(
        "--disabled", action="store_true", help="Add the feed paused (enabled=0)."
    )
    cal_sub.add_parser(
        "list-sources", help="List the user's ICS feeds (never the URLs)."
    )
    c_rm = cal_sub.add_parser("remove-source", help="Delete an ICS feed.")
    c_rm.add_argument("--account", required=True, help="Feed slug.")
    c_sync = cal_sub.add_parser(
        "sync", help="Fetch + parse the user's ICS feeds into commitments."
    )
    c_sync.add_argument(
        "--all-users",
        action="store_true",
        help="Fan out over every active user (each user's own feeds).",
    )
    p_cal.set_defaults(func=_cmd_calendar)

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
