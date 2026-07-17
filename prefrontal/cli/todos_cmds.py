"""``prefrontal`` todo & scheduling commands.

Open-loop and time commands: ``todo`` (add/list/close), ``blocked``, ``fit`` /
``find-time`` (free-window fitting), ``place``, ``clarify``, ``crunch``,
``communicate``, and the ``cleanup`` maintenance passes.
"""

from __future__ import annotations

import argparse
import sys

from prefrontal.clarify import MAX_SWEEP_ITEMS
from prefrontal.cli._common import _resolve_user_store
from prefrontal.clock import TS_FMT
from prefrontal.config import get_settings
from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore
from prefrontal.scheduling import fit_todos
from prefrontal.todos import (
    heuristic_category,
    reclassify_hygiene_drops,
    record_todo_closed,
    resolve_category,
)


def register(sub) -> None:
    """Attach the todo/scheduling subcommands."""
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

    p_blocked = sub.add_parser(
        "blocked",
        aliases=["blockers"],
        help="Track who's blocked on you (the ball's in your court).",
    )
    p_blocked.add_argument("--db-path", default=None, help="Override the database path.")
    p_blocked.add_argument("--user", default=None, help="Handle of the user to act on.")
    blocked_sub = p_blocked.add_subparsers(dest="blocked_action", required=True)
    b_add = blocked_sub.add_parser("add", help="Log that someone is waiting on you.")
    b_add.add_argument("person", help="Who is blocked / waiting on you (a name).")
    b_add.add_argument("what", help="The thing they need from you.")
    b_add.add_argument(
        "--priority", type=int, default=1, choices=[0, 1, 2, 3], help="0 low … 3 urgent."
    )
    b_add.add_argument(
        "--deadline", default=None, help="Optional 'needs it by' (YYYY-MM-DD)."
    )
    b_add.add_argument("--notes", default=None, help="Optional free-text detail.")
    b_list = blocked_sub.add_parser("list", help="List who's blocked on you (most pressing first).")
    b_list.add_argument(
        "--all", action="store_true", help="Include resolved blockers, not just open ones."
    )
    b_resolve = blocked_sub.add_parser("resolve", help="Mark a blocker resolved (you delivered).")
    b_resolve.add_argument("blocker_id", type=int)
    b_reopen = blocked_sub.add_parser("reopen", help="Reopen a resolved blocker.")
    b_reopen.add_argument("blocker_id", type=int)
    p_blocked.set_defaults(func=_cmd_blocked)

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
    pl_add.add_argument(
        "--domain",
        default=None,
        help="Life-sphere for focus balance (shop/work/home/kids/personal); a trip "
        "stopping here pre-fills it. Omit to leave unset.",
    )
    place_sub.add_parser("list", help="List curated places (most specific first).")
    p_place.set_defaults(func=_cmd_place)

    p_fit = sub.add_parser("fit", help="Show todos that fit a block of free time.")
    p_fit.add_argument("minutes", type=float, help="Minutes of free time you have.")
    p_fit.add_argument("--db-path", default=None, help="Override the database path.")
    p_fit.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_fit.set_defaults(func=_cmd_fit)

    p_find_time = sub.add_parser(
        "find-time",
        help="Find open calendar slots from a free-text ask (the calendar assistant).",
    )
    p_find_time.add_argument(
        "message",
        nargs="+",
        help='What to schedule, e.g. "45 min for coffee with Sam this week".',
    )
    p_find_time.add_argument(
        "--llm",
        action="store_true",
        help="Use the configured model to parse the ask (else an offline heuristic).",
    )
    p_find_time.add_argument("--db-path", default=None, help="Override the database path.")
    p_find_time.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_find_time.set_defaults(func=_cmd_find_time)

    p_communicate = sub.add_parser(
        "communicate",
        help="Decode, draft, or soften a work message (text only, nothing sent).",
    )
    p_communicate.add_argument(
        "text",
        nargs="+",
        help="The message received (decode), what to say (draft), or your message (soften).",
    )
    p_communicate.add_argument(
        "--mode",
        choices=["decode", "draft", "soften"],
        default="decode",
        help="decode a received message, draft a reply, or soften your own (default decode).",
    )
    p_communicate.add_argument(
        "--register",
        choices=["professional", "warm", "firm", "concise", "friendly"],
        default=None,
        help="Tone for draft/soften (default professional); ignored for decode.",
    )
    p_communicate.add_argument(
        "--llm",
        action="store_true",
        help="Use the configured model (else decode/draft report the model is unavailable).",
    )
    p_communicate.add_argument("--db-path", default=None, help="Override the database path.")
    p_communicate.add_argument("--user", default=None, help="Handle of the user to act on.")
    p_communicate.set_defaults(func=_cmd_communicate)


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
            from prefrontal.focus_balance import normalize_focus_domain

            name = normalize_query(args.name)
            if not name:
                print("Place name is empty after normalization.")
                return 1
            domain = normalize_focus_domain(args.domain)
            place_id = store.add_place(
                name, args.lat, args.lon, label=args.label or args.name, domain=domain
            )
            dom = f", domain={domain}" if domain else ""
            print(f"Saved place #{place_id}: {name} ({args.lat:g}, {args.lon:g}){dom}")
        elif args.place_action == "list":
            places = store.places()
            if not places:
                print("No curated places yet.")
            for p in places:
                label = p.get("label")
                extra = f" — {label}" if label and label != p["name"] else ""
                dom = f"  [{p['domain']}]" if p.get("domain") else ""
                print(f"{p['name']}{extra}  ({p['lat']:g}, {p['lon']:g}){dom}")
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


def _cmd_blocked(args: argparse.Namespace) -> int:
    """Track who's blocked on you — the ball's in your court.

    A blocker records that someone *else* is waiting on you for something, so it
    can weigh into prioritization (panic mode / the briefing surface it). Capture
    is one line; resolve it once you've delivered.

    Args:
        args: Parsed arguments; ``blocked_action`` plus action-specific fields.

    Returns:
        Process exit code (0 on success, 1 on a not-found resolve/reopen).
    """
    from prefrontal.blockers import describe_blocker, normalize_person

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        if args.blocked_action == "add":
            person = normalize_person(args.person)
            what = (args.what or "").strip()
            if not person or not what:
                print("Both a person and a 'what' are required.", file=sys.stderr)
                return 1
            bid = store.add_blocker(
                person,
                what,
                priority=args.priority,
                deadline=args.deadline,
                notes=args.notes,
            )
            print(f"Logged blocker #{bid}: {person} is waiting on you — {what}")
        elif args.blocked_action == "list":
            blockers = store.list_blockers(include_resolved=args.all)
            if not blockers:
                print("Nobody's blocked on you. 🎉")
            now = utcnow()
            for b in blockers:
                mark = " ✓ resolved" if b.get("status") == "resolved" else ""
                pri = f" [P{b['priority']}]" if b.get("priority") is not None else ""
                print(f"#{b['id']}{pri} {describe_blocker(b, now)}{mark}")
        elif args.blocked_action == "resolve":
            if store.resolve_blocker(args.blocker_id) is not None:
                print(f"Blocker #{args.blocker_id} resolved. ✓")
            else:
                print(f"No blocker #{args.blocker_id}.", file=sys.stderr)
                return 1
        elif args.blocked_action == "reopen":
            if store.reopen_blocker(args.blocker_id) is not None:
                print(f"Blocker #{args.blocker_id} reopened.")
            else:
                print(f"No blocker #{args.blocker_id}.", file=sys.stderr)
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


def _cmd_find_time(args: argparse.Namespace) -> int:
    """Find open calendar slots from a free-text ask (the calendar assistant).

    "find 45 min for coffee with Sam this week", or "when are my wife and I both
    free for dinner tomorrow evening?" — parses duration + timeframe + who's
    involved, then lists open windows. A partner's FYI events block only when the
    plan involves them; otherwise they're ignored. Prints a single clarifying
    question when the ask is too vague (usually a missing duration). Read-only.

    Args:
        args: Parsed arguments; uses ``message``, ``db_path``, ``user``, ``llm``.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.availability import plan_availability, render_plan
    from prefrontal.integrations import ProviderResolver
    from prefrontal.scheduling import window_config_for

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    message = " ".join(args.message).strip()
    if not message:
        print(
            'Say what you want to schedule, e.g. `find-time "45 min for a call this week"`.',
            file=sys.stderr,
        )
        return 2
    with MemoryStore.open(db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        # Claude when the ``assistant`` agent is opted into Anthropic, else local
        # Ollama; the module falls back to an offline heuristic if neither replies.
        client = ProviderResolver.from_settings(settings).client("assistant") if args.llm else None
        cfg = window_config_for(settings, store)
        plan = plan_availability(
            message,
            store,
            client=client,
            now=utcnow(),
            tz=settings.timezone,
            awake_band=cfg.awake_band(),
            band_for_weekday=cfg.band_for_weekday,
        )
    print(render_plan(plan, settings.timezone))
    return 0


def _cmd_communicate(args: argparse.Namespace) -> int:
    """Decode, draft, or soften a work message (communication translation, M4).

    Text only — nothing is sent, booked, or stored. ``--mode decode`` explains
    what a received message really means; ``--mode draft`` writes a reply from a
    short description; ``--mode soften`` rewrites your own message in ``--register``
    (professional/warm/firm/concise/friendly). The model does the work under
    ``--llm`` (Claude when the assistant agent is opted into Anthropic, else local
    Ollama); without a reachable model, decode/draft say so plainly rather than
    guessing.

    Args:
        args: Parsed arguments; uses ``text``, ``mode``, ``register``, ``db_path``,
            ``user``, ``llm``.

    Returns:
        Process exit code (0 on success, 2 on empty input).
    """
    from prefrontal.communication_translation import translate
    from prefrontal.integrations import ProviderResolver

    settings = get_settings()
    db_path = args.db_path or settings.db_path
    text = " ".join(args.text).strip()
    if not text:
        print(
            'Say what to work on, e.g. `communicate --mode soften "just send it already"`.',
            file=sys.stderr,
        )
        return 2
    # The store is opened only so the command shares the standard user-scoping and
    # config path; translation itself is stateless (text in, text out).
    with MemoryStore.open(db_path) as unscoped:
        _resolve_user_store(unscoped, args.user)
        client = ProviderResolver.from_settings(settings).client("assistant") if args.llm else None
        result = translate(text, args.mode, args.register, client=client)
    if result.note:
        print(result.note, file=sys.stderr)
    if result.output:
        print(result.output)
    return 0


