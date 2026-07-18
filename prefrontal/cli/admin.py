"""``prefrontal`` admin & operator commands.

Provisioning and ops: init-db, secrets, user, household (+ its shared-sheet
sub-CLIs: shopping/chores/routines/away/service-shift/balance/digest/stars/…),
migrate, serve, update, restart, and the n8n workflow helpers.
"""

from __future__ import annotations

import argparse
import sys

from prefrontal.cli._common import (
    _print_qr,
    _resolve_user_store,
    build_connect_link,
)
from prefrontal.clock import TS_FMT
from prefrontal.config import get_settings
from prefrontal.household import build_sheet, render_sheet
from prefrontal.impact import utcnow
from prefrontal.memory.db import init_db
from prefrontal.memory.migrate import migrate_to_multi_tenant
from prefrontal.memory.store import MemoryStore, provision_user


def register(sub) -> None:
    """Attach the admin/operator subcommands."""
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
        help="Set/show a user's per-user delivery route (their APNs device token).",
    )
    u_route.add_argument("handle", help="The user's handle.")
    u_route.add_argument(
        "--apns-token",
        default=None,
        help="Their iOS device's APNs token (usually registered by the app; pass '' to clear).",
    )
    # ntfy flags feed the dev-only shim (PREFRONTAL_NTFY_DEV) for free-signing
    # builds; inert on a product build.
    u_route.add_argument(
        "--ntfy-topic",
        default=None,
        help="[dev shim] ntfy topic for a free-signing build (pass '' to clear).",
    )
    u_route.add_argument(
        "--ntfy-server", default=None, help="[dev shim] ntfy server override (pass '' to clear)."
    )
    u_route.add_argument(
        "--ntfy-token",
        default=None,
        help="[dev shim] ntfy access token for a protected topic (pass '' to clear).",
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
    h_tci = house_sub.add_parser(
        "trip-checkin-check",
        help="Prompt a parent who's out to post a status to the other.",
    )
    h_tci.add_argument("--user", default=None, help="Handle of a household member.")
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
            from prefrontal.delivery import resolve_route

            scoped = _resolve_user_store(store, args.handle)  # SystemExits if unknown
            # Only the flags actually passed are written; an empty string clears a
            # field (resolve_route treats "" as unset and falls back). All keys are
            # written source="explicit" so the coaching learner never overwrites a
            # deliberately-set route.
            fields = {
                "apns_token": args.apns_token,
                # ntfy targets feed the dev-only shim (PREFRONTAL_NTFY_DEV); on a
                # product build they're inert.
                "ntfy_topic": args.ntfy_topic,
                "ntfy_server": args.ntfy_server,
                "ntfy_token": args.ntfy_token,
            }
            changed = {k: v for k, v in fields.items() if v is not None}
            for key, value in changed.items():
                scoped.set_state(key, value.strip(), source="explicit")
            route = resolve_route(scoped, settings)
            shown = lambda s: "set" if s else "—"  # noqa: E731 — never print secret values
            print(f"Delivery route for '{args.handle}':")
            print(
                f"  apns        device token {shown(route.apns_token)}  "
                "(native push — the product transport)"
            )
            topic = route.ntfy_topic or "(unset)"
            print(f"  ntfy (dev)  {route.ntfy_server}/{topic} · token {shown(route.ntfy_token)}")
            if changed:
                print(
                    f"Updated: {', '.join(sorted(changed))}. "
                    f"Verify with `prefrontal notify --user {args.handle}`."
                )
        elif args.user_action == "connect-link":
            from prefrontal.delivery import resolve_route

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
            # Native APNs push is the product path (the app registers its device
            # token on first launch), so the connect QR carries no ntfy hints —
            # except on a dev box running the ntfy shim, where they prefill the
            # free-signing notifications step.
            link = build_connect_link(
                base_url,
                token=token,
                ntfy_server=route.ntfy_server if settings.ntfy_dev and route.ntfy_topic else None,
                ntfy_topic=route.ntfy_topic if settings.ntfy_dev else None,
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
        elif args.household_action == "trip-checkin-check":
            return _trip_checkin_cli(store, args, settings)
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


def _trip_checkin_cli(store, args, settings) -> int:
    """Prompt any parent who's out on a trip to post a status to the other.

    The CLI twin of ``POST /webhooks/household/trip-checkin/check`` — for a launchd
    trigger or a manual test. Self-gating: silent when the feature's off, no one's
    out, or the current trip was already prompted.
    """
    from prefrontal.household import run_trip_checkin_sweep

    scoped = _resolve_user_store(store, args.user)
    if scoped.household_id_or_none() is None:
        print("That user isn't in a household.", file=sys.stderr)
        return 1
    result = run_trip_checkin_sweep(scoped, settings=settings)
    if result["reason"] == "not_shared":
        print("Single-parent household — the trip check-in is off.")
        return 0
    if result["reason"] == "disabled":
        print("The trip check-in is off for this household.")
        return 0
    for item in result["sent"]:
        row = item["delivery"]
        state = "sent" if row["delivered"] else "not sent"
        print(f"  → {item['handle']}: trip {item['trip_id']} — {state} ({row['detail']})")
    if not result["sent"]:
        print("No trip check-ins to send (no one's out past the threshold).")
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


