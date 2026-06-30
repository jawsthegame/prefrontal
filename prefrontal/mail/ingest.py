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
from prefrontal.mail.triage import MailTriage, triage_message
from prefrontal.memory.store import MemoryStore

if TYPE_CHECKING:
    from prefrontal.integrations.ollama import OllamaClient


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
        triaged_by_llm: How many verdicts came from the model (vs the heuristic).
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
    triaged_by_llm: int = 0
    message_ids: list[str] = field(default_factory=list)


def ingest_messages(
    store: MemoryStore,
    raw_messages: list[dict[str, Any]],
    *,
    account: str,
    policy: str = "full",
    client: OllamaClient | None = None,
    fallback: bool = True,
    use_model: bool = True,
    create_todos: bool = True,
) -> IngestSummary:
    """Normalize, dedup, triage, and persist a batch of raw messages.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        raw_messages: Loosely-shaped message dicts (see
            :func:`prefrontal.mail.models.normalize_message`).
        account: The logical account these belong to.
        policy: Retention policy (``full`` or ``signals``).
        client: Ollama client for triage. Defaults to one from settings; if the
            model is down, triage falls back to the heuristic (when ``fallback``).
        fallback: Passed through to :func:`triage_message`.
        use_model: When ``False``, triage every message with the keyword
            heuristic instead of the model (fast backlog clear).
        create_todos: When ``True`` (default), create a todo for each
            needs-action message and link it on the mail row.

    Returns:
        An :class:`IngestSummary`.
    """
    seen = store.seen_mail_ids(account)
    summary = IngestSummary(account=account, policy=policy, received=len(raw_messages))

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
            item, client=client, fallback=fallback, use_model=use_model
        )

        todo_id = None
        if create_todos and verdict.needs_action:
            todo_id = store.add_todo(
                _todo_title(item, verdict),
                notes=_todo_notes(item, verdict),
                priority=verdict.priority,
            )
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
