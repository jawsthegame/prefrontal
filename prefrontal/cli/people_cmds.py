"""``prefrontal people`` / ``prefrontal proposals`` — people queue & sensor proposals.

Review surfaces: the people-mention identify/dismiss queue and the sensor's
pending state proposals (accept/reject).
"""

from __future__ import annotations

import argparse
import sys

from prefrontal.cli._common import _resolve_user_store
from prefrontal.config import get_settings
from prefrontal.memory.store import MemoryStore


def register(sub) -> None:
    """Attach the ``proposals`` and ``people`` subcommands."""
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

    p_people = sub.add_parser(
        "people",
        help="Identify & categorize people named in ingested items (learning + priority).",
    )
    p_people.add_argument("--db-path", default=None, help="Override the database path.")
    p_people.add_argument("--user", default=None, help="Handle of the user to act on.")
    people_sub = p_people.add_subparsers(dest="people_action", required=True)
    people_sub.add_parser("queue", help="List names awaiting review (the queue).")
    people_sub.add_parser("list", help="List the identified roster (most-mentioned first).")
    pe_add = people_sub.add_parser("add", help="Add someone to the roster directly.")
    pe_add.add_argument("name", help="The person's name.")
    pe_add.add_argument(
        "--relationship", default="unknown",
        help="family|coworker|friend|professional|service|acquaintance|other|unknown.",
    )
    pe_add.add_argument(
        "--importance", type=int, default=1, choices=range(0, 4),
        help="0 low · 1 normal · 2 high · 3 top.",
    )
    pe_add.add_argument("--notes", default=None, help="Who they are / why they matter.")
    pe_cat = people_sub.add_parser("categorize", help="Recategorize a roster person.")
    pe_cat.add_argument("id", type=int, help="Person id.")
    pe_cat.add_argument("--relationship", default=None, help="New relationship.")
    pe_cat.add_argument(
        "--importance", type=int, default=None, choices=range(0, 4), help="New importance (0–3)."
    )
    pe_cat.add_argument("--notes", default=None, help="New notes.")
    pe_id = people_sub.add_parser(
        "identify", help="Resolve a queued name: link an existing person or create one."
    )
    pe_id.add_argument("id", type=int, help="Mention id.")
    pe_id.add_argument(
        "--person", type=int, default=None, help="Link this existing roster person id."
    )
    pe_id.add_argument("--name", default=None, help="Override the captured name (when creating).")
    pe_id.add_argument(
        "--relationship", default="unknown", help="Relationship (when creating a new person)."
    )
    pe_id.add_argument(
        "--importance", type=int, default=1, choices=range(0, 4),
        help="Importance 0–3 (when creating).",
    )
    pe_id.add_argument("--notes", default=None, help="Notes (when creating).")
    pe_dismiss = people_sub.add_parser("dismiss", help="Dismiss a queued name (not a person).")
    pe_dismiss.add_argument("id", type=int, help="Mention id.")
    pe_scan = people_sub.add_parser("scan", help="Scan free text for names and queue them.")
    pe_scan.add_argument("text", help="Free text to scan for people-names.")
    p_people.set_defaults(func=_cmd_people)


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
        compute_proposal_durability,
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
            resolved = store.all_resolved_proposals()
            # Precision (accept-rate) and durability (do accepted settings stick?)
            # have independent sample gates — durability can be ready on fewer
            # resolved proposals than precision — so print each on its own terms.
            cal = compute_sensor_calibration(resolved)
            if cal.status == "ok":
                print(
                    f"Sensor precision: {cal.accepted}/{cal.resolved} accepted ({cal.accept_rate})."
                )
                for tp in cal.by_target:
                    flag = "  ⚠ chronically rejected" if tp.target in cal.flagged else ""
                    print(
                        f"  {tp.target}: {tp.accepted}/{tp.resolved} "
                        f"({round(tp.accept_rate, 2)}){flag}"
                    )
            else:
                print(
                    f"Not enough resolved proposals yet ({cal.resolved}; "
                    f"need {MIN_SENSOR_CALIBRATION_SAMPLES}) to judge sensor precision."
                )
            # Post-acceptance durability: did accepted settings stick or get reversed?
            dur = compute_proposal_durability(resolved, store.all_state())
            if dur.status == "ok":
                print(
                    f"Sensor durability: {dur.held_up}/{dur.evaluated} accepted settings "
                    f"still standing ({dur.durability_rate})."
                )
                for t in dur.reversed_targets:
                    print(f"  {t}: reversed since accepting")
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


def _cmd_people(args: argparse.Namespace) -> int:
    """Work the people queue: review named mentions, identify/categorize the roster.

    Ingested items keep naming people; the unknown ones queue for review. This
    command lists that queue, identifies a name (link an existing person or create
    + categorize a new one), dismisses a non-person, and manages the roster whose
    relationship/importance feed learning and prioritization. See
    :mod:`prefrontal.people`.

    Args:
        args: Parsed arguments; uses ``db_path``, ``user``, ``people_action`` and
            the per-action fields.

    Returns:
        Process exit code (0 on success, 1 when an id isn't found / is resolved).
    """
    from prefrontal.people import (
        RELATIONSHIPS,
        describe_mention,
        describe_person,
        enqueue_mentions,
        name_key,
        normalize_name,
    )

    settings = get_settings()
    with MemoryStore.open(args.db_path or settings.db_path) as unscoped:
        store = _resolve_user_store(unscoped, args.user)
        action = args.people_action
        if action == "queue":
            pending = store.list_person_mentions("pending")
            if not pending:
                print("Nobody new in the queue. 🎉")
                return 0
            for m in pending:
                print(f"#{m['id']}  {describe_mention(m)}")
            return 0
        if action == "list":
            roster = store.list_people(status="active")
            if not roster:
                print("No one on the roster yet.")
                return 0
            for p in roster:
                print(f"#{p['id']}  {describe_person(p)}")
            return 0
        if action == "add":
            if args.relationship not in RELATIONSHIPS:
                print(
                    f"relationship must be one of: {', '.join(RELATIONSHIPS)}.",
                    file=sys.stderr,
                )
                return 1
            name = normalize_name(args.name)
            if not name:
                print("A person needs a name.", file=sys.stderr)
                return 1
            if store.find_person(name_key(name)) is not None:
                print(f"{name!r} is already on the roster.", file=sys.stderr)
                return 1
            pid = store.add_person(
                name=name, name_key=name_key(name),
                relationship=args.relationship, importance=args.importance,
                notes=args.notes,
            )
            print(f"Added #{pid}: {describe_person(store.get_person(pid))}")
            return 0
        if action == "categorize":
            person = store.get_person(args.id)
            if person is None:
                print(f"No person #{args.id}.", file=sys.stderr)
                return 1
            fields: dict[str, object] = {}
            if args.relationship is not None:
                if args.relationship not in RELATIONSHIPS:
                    print(
                        f"relationship must be one of: {', '.join(RELATIONSHIPS)}.",
                        file=sys.stderr,
                    )
                    return 1
                fields["relationship"] = args.relationship
            if args.importance is not None:
                fields["importance"] = args.importance
            if args.notes is not None:
                fields["notes"] = args.notes
            updated = store.update_person(args.id, **fields)
            print(f"Updated #{args.id}: {describe_person(updated)}")
            return 0
        if action == "scan":
            result = enqueue_mentions(store, text=args.text, source="manual")
            print(
                f"Scanned: {len(result['names'])} name(s) — "
                f"{len(result['queued'])} queued, {len(result['known'])} already known."
            )
            return 0
        if action == "dismiss":
            if not store.dismiss_person_mention(args.id):
                print(f"No pending mention #{args.id}.", file=sys.stderr)
                return 1
            print(f"Dismissed mention #{args.id}.")
            return 0
        # identify
        mention = store.get_person_mention(args.id)
        if mention is None or mention["status"] != "pending":
            print(f"No pending mention #{args.id}.", file=sys.stderr)
            return 1
        if args.person is not None:
            person = store.get_person(args.person)
            if person is None:
                print(f"No person #{args.person}.", file=sys.stderr)
                return 1
            person_id = int(person["id"])
        else:
            if args.relationship not in RELATIONSHIPS:
                print(
                    f"relationship must be one of: {', '.join(RELATIONSHIPS)}.",
                    file=sys.stderr,
                )
                return 1
            name = normalize_name(args.name or mention["name"])
            existing = store.find_person(name_key(name))
            if existing is not None:
                person_id = int(existing["id"])
            else:
                person_id = store.add_person(
                    name=name, name_key=name_key(name),
                    relationship=args.relationship, importance=args.importance,
                    notes=args.notes,
                )
        store.identify_person_mention(args.id, person_id)
        store.touch_person(person_id)
        print(f"Identified #{args.id} → {describe_person(store.get_person(person_id))}")
    return 0


