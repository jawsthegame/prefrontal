"""``prefrontal calendar`` / ``prefrontal care-recipient`` — feeds & care roster.

Per-user data config: private ICS calendar feeds (add/list/remove/sync) and the
care-recipient names roster that drives the deterministic ``care`` classification.
"""

from __future__ import annotations

import argparse
import sys

from prefrontal.cli._common import _resolve_user_store, _user_targets
from prefrontal.config import get_settings
from prefrontal.memory.store import MemoryStore


def register(sub) -> None:
    """Attach the ``calendar`` and ``care-recipient`` subcommands."""
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

    p_care = sub.add_parser(
        "care-recipient",
        help="Manage the care-recipient names roster (drives 'care' classification).",
    )
    p_care.add_argument("--db-path", default=None, help="Override the database path.")
    p_care.add_argument("--user", default=None, help="Handle of the user to scope to.")
    care_sub = p_care.add_subparsers(dest="care_action", required=True)
    care_sub.add_parser("list", help="Print the current care-recipient roster.")
    c_set = care_sub.add_parser("set", help="Replace the roster with these names.")
    c_set.add_argument("names", nargs="*", help="Care-recipient names (empty clears the roster).")
    c_add = care_sub.add_parser("add", help="Add names to the roster.")
    c_add.add_argument("names", nargs="+", help="Care-recipient names to add.")
    c_rm = care_sub.add_parser("remove", help="Remove names from the roster.")
    c_rm.add_argument("names", nargs="+", help="Care-recipient names to remove.")
    p_care.set_defaults(func=_cmd_care_recipient)


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
            fetch_failed = False
            for feed in feeds:
                try:
                    text = fetch_ics(feed.url, timeout=settings.ics_fetch_timeout)
                except Exception as exc:  # httpx raises a variety of errors
                    print(
                        f"[{handle}/{feed.account}] ICS fetch failed: {exc}",
                        file=sys.stderr,
                    )
                    status = 1
                    fetch_failed = True
                    continue
                events.extend(
                    parse_ics(text, namespace=feed.namespace, me_emails=feed.me_emails)
                )

            # Don't let a fetch failure masquerade as an empty calendar. When a
            # feed times out (or otherwise errors) it contributes no events; if
            # that leaves the whole batch empty, sync_calendar would prune every
            # existing calendar commitment as "missing" — a transient network
            # blip would silently wipe the calendar. Skip the sync entirely and
            # leave the stored events untouched until a feed actually responds.
            # (A genuinely empty batch with no fetch error still prunes normally.)
            if fetch_failed and not events:
                print(
                    f"[{handle}] calendar sync skipped: all feeds failed to fetch; "
                    "leaving existing events untouched.",
                    file=sys.stderr,
                )
                continue

            # The roster pass is deterministic/offline, so a kid's appointment
            # still lands on the shared sheet as 'child' even when Ollama is down;
            # build the classifier whenever either signal is available.
            child_names = store.child_names()
            care_names = store.care_recipient_names()
            examples = store.kind_feedback_examples() if ollama_up else None
            classify = None
            if ollama_up or child_names or care_names:

                def classify(title, _c=ollama if ollama_up else None,
                             _ex=examples, _names=child_names, _care=care_names):
                    return classify_kind(
                        title, client=_c, examples=_ex,
                        child_names=_names, care_names=_care,
                    )

            try:
                summary = sync_calendar(
                    store, events, classify=classify, default_tz=settings.timezone,
                    recur_horizon_hours=settings.calendar_horizon_days * 24.0,
                    recur_min_occurrences=settings.calendar_min_occurrences,
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


def _cmd_care_recipient(args: argparse.Namespace) -> int:
    """Manage the care-recipient names roster (adults the user looks after).

    The per-user roster that drives the deterministic ``care`` calendar
    classification — the caregiver counterpart to household kids' names. ``list``
    prints the current roster; ``set`` replaces it; ``add`` / ``remove`` edit it
    incrementally. All writes go through the same normalization (trim blanks,
    de-duplicate case-insensitively) the classifier reads.

    Args:
        args: Parsed arguments; ``care_action`` plus ``names`` / ``--user``.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as store:
        scoped = _resolve_user_store(store, args.user)
        current = scoped.care_recipient_names()
        action = args.care_action
        if action == "list":
            pass  # fall through to the shared print
        elif action == "set":
            current = scoped.set_care_recipient_names(args.names)
        elif action == "add":
            current = scoped.set_care_recipient_names(current + args.names)
        elif action == "remove":
            drop = {n.strip().lower() for n in args.names}
            current = scoped.set_care_recipient_names(
                [n for n in current if n.lower() not in drop]
            )
        if current:
            print("Care recipients: " + ", ".join(current))
        else:
            print("No care recipients set.")
    return 0


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


