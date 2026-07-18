"""``prefrontal mail`` — ingest / fetch / list triaged email.

Wraps IMAP fetch + the mail ingest pipeline (dedup, triage, todo creation) and
the list/summary views, with the shared ingest helpers the command uses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prefrontal.cli._common import _resolve_user_store, _user_targets
from prefrontal.config import get_settings
from prefrontal.mail.imap import DEFAULT_UNSEEN_WINDOW_DAYS
from prefrontal.memory.store import MemoryStore


def register(sub) -> None:
    """Attach the ``mail`` subcommand."""
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


