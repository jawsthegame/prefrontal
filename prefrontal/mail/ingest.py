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

from prefrontal.mail.models import MailItem, normalize_message
from prefrontal.mail.triage import MailTriage, suppress_todo_reason, triage_message
from prefrontal.memory.store import MemoryStore
from prefrontal.projects import suggest_project

if TYPE_CHECKING:
    from prefrontal.integrations import Generator
    from prefrontal.triage import Signal, TriageDecision


# --- Bridge into the one shared triage pipeline ------------------------------
#
# Mail keeps its own specialized *classifier* (``triage_message`` — retention,
# categories, waiting-on, denylist/corrections), but it no longer routes or
# audits on its own: an actionable verdict is expressed as a generic
# :class:`~prefrontal.triage.Signal` + :class:`~prefrontal.triage.TriageDecision`
# and handed to the shared :func:`prefrontal.triage.apply`, so there is *one* place
# that creates the todo and *one* ``triage_log``. This is the "one triage, not two"
# unification (docs/triage-agent.md reality note): a new ingestion source brings a
# ``Signal`` and rides the same pipeline. Delivery (the ``triage.urgent`` nudge)
# stays out of the mail path for now — mail passes no n8n client — so this is a
# routing/audit unification, not a behavior change to what mail notifies.


def _mail_signal(item: MailItem, verdict: MailTriage, account: str) -> Signal:
    """Express a triaged message as the source-agnostic :class:`Signal`.

    ``title`` mirrors what the mail audit row used before unification (subject,
    then the model's summary, then a placeholder) so ``GET /triage/recent`` reads
    the same. ``external_id`` is the account-scoped message id, so the shared
    ``triage_log`` unique index dedups a re-delivered email exactly as the mail
    dedup already does upstream.
    """
    # Imported lazily: prefrontal.delegation (imported at module top) reaches
    # prefrontal.mail via sources→imap, so an eager triage import here re-enters a
    # partially-initialized mail package when delegation is imported first.
    from prefrontal.triage import Signal

    return Signal(
        source="mail",
        title=item.subject or verdict.summary or "(no subject)",
        body=item.body or item.snippet or "",
        sender=item.sender or item.sender_email or "",
        received_at=item.received_at or "",
        external_id=f"{account}:{item.message_id}",
        meta={"account": account, "category": verdict.category},
    )


def _mail_decision(
    verdict: MailTriage,
    *,
    matched: bool,
    route: str,
    todo_payload: dict[str, Any] | None = None,
    routed_ref: str | None = None,
) -> TriageDecision:
    """Map a :class:`MailTriage` verdict onto a generic :class:`TriageDecision`.

    ``kind`` is always ``action`` (only actionable/matched mail reaches here);
    ``urgency`` collapses the mail 0–3 priority to the shared ladder (``now`` for
    urgent, else ``today``); ``reason`` reproduces the pre-unification mail audit
    string. The specialized routed row is carried in ``fields``: ``todo`` (a
    pre-built payload the shared router creates verbatim, no re-augmentation) for a
    fresh todo, or ``routed_ref`` (an existing ``todo:<id>``) when mail has already
    linked the row — e.g. closing a delegation loop.
    """
    from prefrontal.triage import TriageDecision  # lazy — see _mail_signal

    reason = (
        f"mail: {verdict.category}"
        + (" · delegation returned" if matched else "")
        + (f" · waiting on {verdict.waiting_on}" if verdict.waiting_on else "")
        + ("" if route == "todo" else " · todo suppressed")
    )
    fields: dict[str, Any] = {}
    if todo_payload is not None:
        fields["todo"] = todo_payload
    if routed_ref is not None:
        fields["routed_ref"] = routed_ref
    return TriageDecision(
        kind="action",
        urgency="now" if verdict.priority >= 3 else "today",
        route=route,
        reason=reason,
        confidence=0.9 if verdict.source == "llm" else 0.6,
        source=verdict.source,
        fields=fields,
    )


def _mail_todo_payload(
    store: MemoryStore,
    item: MailItem,
    verdict: MailTriage,
    *,
    client: Generator | None,
    domain: str | None,
) -> dict[str, Any]:
    """Build the todo the shared router will create for a needs-action message.

    The mail-specialized title/notes/priority/domain/project — the values mail
    computed inline before unification — packaged so ``triage.apply`` creates the
    row verbatim. ``source="manual"`` preserves the pre-unification provenance
    (mail todos were indistinguishable from hand-added ones; the notes still carry
    the ``[mail/<account>]`` origin).
    """
    title = _todo_title(item, verdict)
    notes = _todo_notes(item, verdict)
    project_id = suggest_project(title, notes, store.active_projects(), client=client)
    return {
        "title": title,
        "notes": notes,
        "priority": verdict.priority,
        "domain": domain,
        "project_id": project_id,
        "source": "manual",
    }


def mail_todo_payload_from_row(
    store: MemoryStore,
    row: dict[str, Any],
    *,
    client: Generator | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """Build a todo payload for an *already-stored* mail message.

    The manual counterpart to :func:`_mail_todo_payload`: that one runs at ingest
    with the live ``MailItem``/``MailTriage`` in hand, whereas this one serves the
    on-demand ``POST /mail/{id}/todo`` path, where all we have is the persisted
    ``mail_messages`` row (a needs-action message whose todo was suppressed). The
    two value objects are reconstructed from the row so the resulting title/notes/
    priority match what ingest would have produced.
    """
    unread = row.get("unread")
    item = MailItem(
        account=row.get("account") or "",
        message_id=row.get("message_id") or "",
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
    verdict = MailTriage(
        needs_action=bool(row.get("needs_action")),
        urgency=row.get("urgency") or "normal",
        category=row.get("category") or "other",
        waiting_on=row.get("waiting_on"),
        summary=row.get("summary") or "",
        source=row.get("triage_source") or "heuristic",
    )
    return _mail_todo_payload(store, item, verdict, client=client, domain=domain)


def _todo_id_from_ref(routed_ref: str | None) -> int | None:
    """Extract the integer id from a ``"todo:<id>"`` routed-ref, or ``None``."""
    if isinstance(routed_ref, str) and routed_ref.startswith("todo:"):
        try:
            return int(routed_ref.split(":", 1)[1])
        except ValueError:
            return None
    return None


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
    # Lazy: prefrontal.delegation imports prefrontal.sources → prefrontal.mail.imap,
    # so importing delegation at this module's top re-enters a partially-initialized
    # mail package when delegation is imported before mail (a collection-order cycle).
    from prefrontal.delegation import STATUS_RETURNED, match_delegated_reply

    seen = store.seen_mail_ids(account)
    summary = IngestSummary(account=account, policy=policy, received=len(raw_messages))

    # Resolve the model once so triage and the delegation loop-closer share it
    # (mirrors triage_message's own default). Routes through the provider selector
    # so the mail/triage agent honors ANTHROPIC_AGENTS instead of always going
    # local; falls back to Ollama when it's not opted in / unavailable.
    if use_model and client is None:
        from prefrontal.integrations.provider import TRIAGE, ProviderResolver

        client = ProviderResolver.from_settings().client(TRIAGE)

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

        # Decide the routed outcome, then hand it to the *one* shared triage
        # pipeline (prefrontal.triage.apply), which both creates the todo and writes
        # the triage_log row. Mail no longer calls add_todo / log_triage itself —
        # this is the "one triage, not two" unification. Only needs-action or
        # delegation-matched mail is routed (ordinary informational mail stays in
        # /mail so the feed and the briefing's "worth a look" aren't flooded).
        todo_id = None
        route = "drop"
        todo_payload: dict[str, Any] | None = None
        routed_ref: str | None = None
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
            route = "todo"
            routed_ref = f"todo:{todo_id}"  # link the existing todo; don't re-create
        elif create_todos and verdict.needs_action:
            if suppress_todo_reason(
                item, verdict, denylisted_senders=denylisted_senders
            ) is None:
                todo_payload = _mail_todo_payload(
                    store, item, verdict, client=client, domain=domain
                )
                route = "todo"
            else:
                summary.todos_suppressed += 1

        # Route + audit through the shared pipeline for anything actionable. A
        # suppressed (or create_todos=False) needs-action message still logs as a
        # `drop`, exactly as before. n8n is intentionally omitted (no mail-urgent
        # nudge yet).
        if verdict.needs_action or matched is not None:
            from prefrontal.triage import apply as triage_apply  # lazy — see _mail_signal

            result = triage_apply(
                _mail_signal(item, verdict, account),
                _mail_decision(
                    verdict,
                    matched=matched is not None,
                    route=route,
                    todo_payload=todo_payload,
                    routed_ref=routed_ref,
                ),
                store,
                n8n=None,
                client=None,  # payload is pre-built, so no augmentation client needed
            )
            if todo_payload is not None:
                todo_id = _todo_id_from_ref(result.get("routed_ref"))
                summary.todos_created += 1

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
                # Route through the shared triage pipeline, exactly as
                # ingest_messages does, so a newly-flagged todo is created *and*
                # writes a triage_log row — otherwise GET /triage/recent would be
                # inconsistent depending on whether a todo came from ingest or
                # retriage. `apply` is idempotent on (source, external_id): if this
                # message was already logged, it returns the prior ref instead of
                # double-logging.
                from prefrontal.triage import apply as triage_apply

                todo_payload = _mail_todo_payload(
                    store, item, verdict, client=client, domain=domain
                )
                result = triage_apply(
                    _mail_signal(item, verdict, row["account"]),
                    _mail_decision(
                        verdict, matched=False, route="todo", todo_payload=todo_payload
                    ),
                    store,
                    n8n=None,
                    client=None,  # payload is pre-built; no augmentation needed
                )
                routed_id = _todo_id_from_ref(result.get("routed_ref"))
                if routed_id is not None:
                    todo_id = routed_id
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
