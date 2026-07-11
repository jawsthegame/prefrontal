"""Ingest raw messages: dedup, triage, and fold into the memory layer.

This is the orchestration tying :mod:`prefrontal.mail.models` and
:mod:`prefrontal.mail.triage` to the store. For each raw message it:

1. **Normalizes** it (applying the account's retention policy).
2. **Dedups** on the account-scoped ``message_id`` — re-syncing an inbox is
   idempotent, so n8n can post overlapping batches freely.
3. **Triages** it (local model, heuristic fallback).
4. **Persists** a ``mail_messages`` row, a ``mail`` episode (an inert record for
   history), and — for anything flagged ``needs_action`` — a **todo**, so the
   message surfaces as an open loop in the briefing and ``prefrontal todo``.

The todo is the bridge into the existing executive-function loop: a triaged "this
needs a reply" becomes the same kind of open loop as anything else Prefrontal
tracks, and closing the todo clears the mail from the action list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prefrontal.delegation import STATUS_RETURNED, match_delegated_reply
from prefrontal.mail.models import MailItem, normalize_message
from prefrontal.mail.triage import MailTriage, suppress_todo_reason, triage_message
from prefrontal.memory.store import MemoryStore
from prefrontal.projects import suggest_project

if TYPE_CHECKING:
    from prefrontal.integrations import Generator


@dataclass
class IngestSummary:
    """Outcome of an :func:`ingest_messages` run.

    Attributes:
        account: The account ingested.
        policy: The retention policy applied.
        received: How many raw messages were handed in.
        ingested: How many new messages were stored.
        skipped: How many were already seen (deduplicated).
        invalid: How many were dropped for lacking a usable id.
        needs_action: How many ingested messages were flagged needing action.
        todos_created: How many todos were created for needs-action items.
        todos_suppressed: How many needs-action items were gated out of todo
            creation (no-reply/notification sender, informational category, or a
            learned repeat-dropped sender). They are still recorded and still
            read as ``needs_action`` in ``/mail`` — they just don't spawn a todo.
        triaged_by_llm: How many verdicts came from the model (vs the heuristic).
        delegations_advanced: How many messages were inferred to be a human VA
            returning a delegated todo, and so advanced that delegation to
            ``returned`` and linked to the existing todo — instead of spawning an
            unrelated new todo (see :func:`prefrontal.delegation.match_delegated_reply`).
        message_ids: The ids of the newly ingested messages, in order.
    """

    account: str
    policy: str
    received: int = 0
    ingested: int = 0
    skipped: int = 0
    invalid: int = 0
    needs_action: int = 0
    todos_created: int = 0
    todos_suppressed: int = 0
    triaged_by_llm: int = 0
    delegations_advanced: int = 0
    message_ids: list[str] = field(default_factory=list)


def ingest_messages(
    store: MemoryStore,
    raw_messages: list[dict[str, Any]],
    *,
    account: str,
    policy: str = "full",
    client: Generator | None = None,
    fallback: bool = True,
    use_model: bool = True,
    create_todos: bool = True,
    corrections: str = "",
    denylisted_senders: frozenset[str] = frozenset(),
    domain: str | None = None,
) -> IngestSummary:
    """Normalize, dedup, triage, and persist a batch of raw messages.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        raw_messages: Loosely-shaped message dicts (see
            :func:`prefrontal.mail.models.normalize_message`).
        account: The logical account these belong to.
        policy: Retention policy (``full`` or ``signals``).
        client: Model client for triage (local Ollama, or Claude when the
            ``triage`` agent is opted into the Anthropic provider). Defaults to
            one from settings; if the model is down, triage falls back to the
            heuristic (when ``fallback``).
        fallback: Passed through to :func:`triage_message`.
        use_model: When ``False``, triage every message with the keyword
            heuristic instead of the model (fast backlog clear).
        create_todos: When ``True`` (default), create a todo for each
            needs-action message and link it on the mail row.
        corrections: A learned-corrections addendum (see
            :func:`prefrontal.mail.feedback.learned_corrections`) appended to the
            triage system prompt, so triage adapts to the user's Drop feedback.
            Passed through to :func:`triage_message`; empty = base prompt.
        denylisted_senders: Lowercased sender emails whose mail must not create a
            todo even when triaged ``needs_action`` (see
            :func:`prefrontal.mail.feedback.learned_denylist`). A deterministic
            gate over the verdict; the message is still recorded.

    Returns:
        An :class:`IngestSummary`.
    """
    seen = store.seen_mail_ids(account)
    summary = IngestSummary(account=account, policy=policy, received=len(raw_messages))

    # Resolve the model once so triage and the delegation loop-closer share it
    # (mirrors triage_message's own default; unchanged behavior when None).
    if use_model and client is None:
        from prefrontal.integrations.ollama import OllamaClient

        client = OllamaClient.from_settings()

    # Inbound delegation loop-closer (#448): a message from a VA we handed a todo
    # off to may be that work coming back. Fetch the open delegations once — the
    # matcher gates cheaply on sender, so this stays inert without any email-handler
    # delegation whose destination matches an incoming sender.
    delegation_candidates = store.actively_delegated_todos() if create_todos else []

    for raw in raw_messages:
        try:
            item = normalize_message(raw, account=account, policy=policy)
        except ValueError:
            summary.invalid += 1
            continue
        if item.message_id in seen:
            summary.skipped += 1
            continue
        seen.add(item.message_id)  # guard against duplicates within one batch

        verdict = triage_message(
            item,
            client=client,
            fallback=fallback,
            use_model=use_model,
            corrections=corrections,
        )

        # Is this the return of something we delegated? If so, close that loop and
        # link the mail to the existing todo rather than spawning a new one.
        matched = (
            match_delegated_reply(
                sender_email=item.sender_email,
                subject=item.subject,
                body=item.body or item.snippet,
                candidates=delegation_candidates,
                client=client if use_model else None,
            )
            if delegation_candidates
            else None
        )

        todo_id = None
        if matched is not None:
            store.update_delegation_status(
                matched["id"],
                STATUS_RETURNED,
                detail=f"Reply from {item.sender_email} via {account}: "
                f"{item.subject or '(no subject)'}",
            )
            todo_id = matched["id"]
            summary.delegations_advanced += 1
            # A single VA reply closes a single loop: drop it so a follow-up message
            # in the same batch can't re-advance the same delegation.
            delegation_candidates = [
                c for c in delegation_candidates if c["id"] != matched["id"]
            ]
        elif create_todos and verdict.needs_action:
            if suppress_todo_reason(
                item, verdict, denylisted_senders=denylisted_senders
            ) is None:
                todo_title = _todo_title(item, verdict)
                todo_notes = _todo_notes(item, verdict)
                project_id = suggest_project(
                    todo_title, todo_notes, store.active_projects(), client=client
                )
                todo_id = store.add_todo(
                    todo_title,
                    notes=todo_notes,
                    priority=verdict.priority,
                    domain=domain,
                    project_id=project_id,
                )
                summary.todos_created += 1
            else:
                summary.todos_suppressed += 1

        store.record_mail(
            account=account,
            message_id=item.message_id,
            policy=policy,
            thread_id=item.thread_id,
            sender_name=item.sender_name,
            sender_email=item.sender_email,
            subject=item.subject,
            received_at=item.received_at,
            snippet=item.snippet,
            body=item.body,
            unread=item.unread,
            needs_action=verdict.needs_action,
            urgency=verdict.urgency,
            category=verdict.category,
            waiting_on=verdict.waiting_on,
            summary=verdict.summary,
            triage_source=verdict.source,
            todo_id=todo_id,
        )

        # Bridge into the unified triage feed: mirror an *actionable* mail verdict
        # into triage_log so it shows in GET /triage/recent, the dashboard Triage
        # panel, and alongside other sources. Mail keeps its own specialized triage
        # (corrections, denylist, retention, needs_action) — this is an audit
        # mirror, not a second classifier. Only needs-action mail is mirrored (a
        # todo, or a suppressed "drop"); ordinary informational mail stays in /mail
        # so the feed and the briefing's "worth a look" aren't flooded. Runs once
        # per message (dedup above), so the triage_log unique index is never hit.
        if verdict.needs_action or matched is not None:
            store.log_triage(
                source="mail",
                title=item.subject or verdict.summary or "(no subject)",
                kind="action",
                urgency="now" if verdict.priority >= 3 else "today",
                route="todo" if todo_id is not None else "drop",
                reason=(
                    f"mail: {verdict.category}"
                    + (" · delegation returned" if matched is not None else "")
                    + (f" · waiting on {verdict.waiting_on}" if verdict.waiting_on else "")
                    + ("" if todo_id is not None else " · todo suppressed")
                ),
                confidence=0.9 if verdict.source == "llm" else 0.6,
                decided_by=verdict.source,
                external_id=f"{account}:{item.message_id}",
                routed_ref=f"todo:{todo_id}" if todo_id is not None else None,
            )
        # An inert record of the message for history; no outcome/ack, so it
        # never pollutes the time-estimation or drift pattern passes.
        store.log_episode(
            "mail",
            channel=account,
            context=f"{verdict.category}:{verdict.urgency}",
            notes=verdict.summary or item.subject,
            timestamp=item.received_at,
        )

        summary.ingested += 1
        summary.message_ids.append(item.message_id)
        if verdict.needs_action:
            summary.needs_action += 1
        if verdict.source == "llm":
            summary.triaged_by_llm += 1

    return summary


@dataclass
class RetriageSummary:
    """Outcome of a :func:`retriage_messages` run.

    Attributes:
        account: The account re-triaged, or ``None`` for all accounts.
        scanned: How many stored messages were re-classified.
        changed: How many got a different verdict (any field).
        cleared: How many flipped ``needs_action`` True → False (the "junk").
        newly_flagged: How many flipped False → True (only possible with a
            full re-triage, i.e. ``only_needs_action=False``).
        todos_dropped: How many open intake todos were dropped for cleared mail.
        todos_created: How many todos were created for newly-flagged mail.
        todos_suppressed: How many newly-flagged messages were gated out of todo
            creation by :func:`prefrontal.mail.triage.suppress_todo_reason`.
        triaged_by_llm: How many verdicts came from the model (vs the heuristic).
        dry_run: Whether this run wrote nothing (a preview).
    """

    account: str | None
    scanned: int = 0
    changed: int = 0
    cleared: int = 0
    newly_flagged: int = 0
    todos_dropped: int = 0
    todos_created: int = 0
    todos_suppressed: int = 0
    triaged_by_llm: int = 0
    dry_run: bool = False


def _item_from_row(row: dict[str, Any]) -> MailItem:
    """Rebuild the :class:`MailItem` for a stored ``mail_messages`` row.

    The row already holds policy-applied content (``signals`` rows have no
    body/snippet), so this is a straight field copy — no re-normalization.
    """
    unread = row.get("unread")
    return MailItem(
        account=row["account"],
        message_id=row["message_id"],
        policy=row.get("policy") or "full",
        thread_id=row.get("thread_id"),
        sender_name=row.get("sender_name"),
        sender_email=row.get("sender_email"),
        subject=row.get("subject"),
        received_at=row.get("received_at"),
        snippet=row.get("snippet"),
        body=row.get("body"),
        unread=bool(unread) if unread is not None else None,
    )


def retriage_messages(
    store: MemoryStore,
    *,
    account: str | None = None,
    only_needs_action: bool = True,
    client: Generator | None = None,
    fallback: bool = True,
    use_model: bool = True,
    create_todos: bool = True,
    corrections: str = "",
    denylisted_senders: frozenset[str] = frozenset(),
    domain: str | None = None,
    dry_run: bool = False,
) -> RetriageSummary:
    """Re-run triage over already-ingested mail with the current prompt.

    Unlike :func:`ingest_messages`, which dedups and skips seen mail, this
    re-classifies stored rows in place. It's how a user reaps a prompt change:
    over-flagged mail that the evolved prompt now considers no-action is cleared
    and its intake todo dropped, unclutter​ing the action list.

    Todos for cleared mail are dropped via :meth:`~MemoryStore.close_todo`
    directly — *not* the ``prefrontal todo drop`` path — so they are **not**
    recorded as triage-drop feedback. A re-triage reflects the prompt's own
    judgment; feeding it back as user corrections would double-count it.

    Args:
        store: An open, user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        account: Limit to one account, or ``None`` for all.
        only_needs_action: When ``True`` (default), only re-triage mail currently
            flagged ``needs_action`` (can only clear junk). When ``False``,
            re-triage everything (can also newly flag previously-cleared mail).
        client: Model client for triage (see :func:`ingest_messages`).
        fallback: Passed through to :func:`triage_message`.
        use_model: When ``False``, re-triage with the keyword heuristic only.
        create_todos: When ``True``, create a todo for newly-flagged mail.
        corrections: Learned-corrections addendum, as in :func:`ingest_messages`.
        denylisted_senders: Sender emails to gate out of todo creation, as in
            :func:`ingest_messages`.
        dry_run: When ``True``, compute and count changes but write nothing.

    Returns:
        A :class:`RetriageSummary`.
    """
    rows = store.mail_for_retriage(account=account, only_needs_action=only_needs_action)
    summary = RetriageSummary(account=account, dry_run=dry_run)

    for row in rows:
        summary.scanned += 1
        item = _item_from_row(row)
        verdict = triage_message(
            item,
            client=client,
            fallback=fallback,
            use_model=use_model,
            corrections=corrections,
        )
        if verdict.source == "llm":
            summary.triaged_by_llm += 1

        was_action = bool(row.get("needs_action"))
        now_action = verdict.needs_action
        todo_id = row.get("todo_id")

        if was_action and not now_action:
            summary.cleared += 1
            if todo_id is not None:
                existing = store.get_todo(todo_id)
                if existing is not None and existing.get("status") == "open":
                    if not dry_run:
                        store.close_todo(todo_id, status="dropped")
                    summary.todos_dropped += 1
        elif now_action and not was_action:
            summary.newly_flagged += 1
            suppressed = suppress_todo_reason(
                item, verdict, denylisted_senders=denylisted_senders
            ) is not None
            if not create_todos or suppressed:
                if suppressed:
                    summary.todos_suppressed += 1
            elif dry_run:
                summary.todos_created += 1
            else:
                todo_title = _todo_title(item, verdict)
                todo_notes = _todo_notes(item, verdict)
                project_id = suggest_project(
                    todo_title, todo_notes, store.active_projects(), client=client
                )
                todo_id = store.add_todo(
                    todo_title,
                    notes=todo_notes,
                    priority=verdict.priority,
                    domain=domain,
                    project_id=project_id,
                )
                summary.todos_created += 1

        if (
            now_action != was_action
            or (row.get("urgency") or None) != (verdict.urgency or None)
            or (row.get("category") or None) != (verdict.category or None)
            or (row.get("waiting_on") or "") != (verdict.waiting_on or "")
            or (row.get("summary") or "") != (verdict.summary or "")
        ):
            summary.changed += 1

        if not dry_run:
            store.update_mail_triage(
                int(row["id"]),
                needs_action=now_action,
                urgency=verdict.urgency,
                category=verdict.category,
                waiting_on=verdict.waiting_on,
                summary=verdict.summary,
                triage_source=verdict.source,
                todo_id=todo_id,
            )

    return summary


def _todo_title(item: MailItem, verdict: MailTriage) -> str:
    """Build a concise todo title for a needs-action message."""
    who = item.sender_name or item.sender_email or "sender"
    subject = item.subject or verdict.summary or "(no subject)"
    if verdict.category == "meeting":
        return f"Respond to invite: {subject}"
    return f"Reply to {who}: {subject}"


def _todo_notes(item: MailItem, verdict: MailTriage) -> str:
    """Build the todo notes — the triage summary plus provenance."""
    parts = []
    if verdict.summary:
        parts.append(verdict.summary)
    parts.append(f"[mail/{item.account}] from {item.sender or 'unknown'}")
    if verdict.waiting_on:
        parts.append(f"waiting on: {verdict.waiting_on}")
    return " — ".join(parts)
