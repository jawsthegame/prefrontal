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
- ``prefrontal todo`` — add/list/done open todos (open loops).
- ``prefrontal fit`` — show open todos that fit N minutes of free time.
- ``prefrontal mail`` — ingest/fetch/list triaged email (list/sync/fetch).

Multi-tenant: the data commands (``learn``, ``summarize``, ``profile``,
``briefing``, ``todo``, ``fit``, ``mail``) act on one user, chosen with
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
from prefrontal.config import get_settings
from prefrontal.impact import utcnow
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
from prefrontal.scheduling import fit_todos
from prefrontal.todos import record_todo_closed


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
            summary = recompute_patterns(s)
            by_type = (
                ", ".join(f"{n} {t}" for t, n in sorted(summary.by_type.items()))
                or "none"
            )
            print(f"[{label}] recomputed patterns from {summary.episodes} episodes.")
            print(f"[{label}] patterns written: {summary.patterns} ({by_type})")
            if summary.bias is not None:
                print(f"[{label}] time_estimation_bias -> {summary.bias}")
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
    from prefrontal.integrations.ollama import OllamaClient, OllamaError

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    client = OllamaClient(
        base_url=settings.ollama_url, model=args.model or settings.ollama_model
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
            except OllamaError as exc:
                print(f"[{handle}] summarization failed: {exc}", file=sys.stderr)
                return 1
            if not args.no_cache:
                cache_summary(s, result)
            if result.source == "heuristic":
                print(
                    f"[{handle}] Ollama unavailable ({client.base_url}, model "
                    f"{client.model}); cached the structured profile instead.",
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
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        if args.llm:
            result = summarize_briefing(store)
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
            todo_id = store.add_todo(
                args.title,
                estimate_minutes=args.minutes,
                priority=args.priority,
                energy=args.energy,
            )
            print(f"Added todo #{todo_id}: {args.title}")
        elif args.todo_action == "list":
            todos = store.open_todos()
            if not todos:
                print("No open todos.")
            for t in todos:
                est = f" ~{t['estimate_minutes']:g}m" if t.get("estimate_minutes") else ""
                print(f"#{t['id']} [P{t['priority']}]{est} {t['title']}")
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
    return 0


def _cmd_fit(args: argparse.Namespace) -> int:
    """Show open todos that fit a block of free time, applying the time bias.

    Args:
        args: Parsed arguments; uses ``minutes`` and ``db_path``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)

        def _state_float(key: str, default: float) -> float:
            raw = store.get_state(key)
            try:
                return float(raw) if raw is not None else default
            except (TypeError, ValueError):
                return default

        bias = _state_float("time_estimation_bias", 1.0)
        fits = fit_todos(args.minutes, store.open_todos(), bias)
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
    from prefrontal.mail.feedback import learned_corrections

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
                dry_run=args.dry_run,
            )
        scope = args.account or "all accounts"
        lead = "[dry-run] would re-triage" if summary.dry_run else "re-triaged"
        print(
            f"[{scope}] {lead} {summary.scanned} message(s): "
            f"{summary.changed} changed, {summary.cleared} cleared "
            f"({summary.todos_dropped} todos dropped), "
            f"{summary.newly_flagged} newly flagged "
            f"({summary.todos_created} todos created), "
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
        )
    print(
        f"[{summary.account}/{summary.policy}] received {summary.received}, "
        f"ingested {summary.ingested}, skipped {summary.skipped}, "
        f"needs-action {summary.needs_action} ({summary.todos_created} todos), "
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
    todo_sub.add_parser("list", help="List open todos.")
    t_done = todo_sub.add_parser("done", help="Mark a todo done.")
    t_done.add_argument("todo_id", type=int)
    t_drop = todo_sub.add_parser("drop", help="Drop a todo.")
    t_drop.add_argument("todo_id", type=int)
    p_todo.set_defaults(func=_cmd_todo)

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
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
