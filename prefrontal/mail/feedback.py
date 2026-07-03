"""Learn from dropped intake todos to evolve the mail-triage prompt.

This closes a feedback loop: mail intake flags an email ``needs_action`` and
creates a todo (:mod:`prefrontal.mail.ingest`); if the user later **drops** that
todo, that's a hint triage over-flagged. :func:`record_drop_feedback` captures
the drop's context, and :func:`learned_corrections` folds the reliable part of
that history back into the triage system prompt as negative few-shot examples.

The subtlety is that a drop is ambiguous — it can also mean the user *avoided*
something they should have done (what :func:`prefrontal.todos.avoided_todos`
surfaces). Feeding every drop to the prompt as "don't flag this" would risk
teaching triage to suppress important-but-avoided mail. So the signal is
filtered to the two cases a drop reliably means a false positive:

- **Quick drops** — dropped within ``quick_drop_days`` of arriving, before the
  todo had time to be avoided.
- **Repeat senders** — a sender dropped ``repeat_threshold``+ times; persistent
  dropping means that sender's mail rarely needs action.

A one-off, slow drop (more likely avoidance) is recorded but never injected.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from prefrontal.clock import parse_ts as _parse_ts
from prefrontal.mail.triage import build_corrections_block

if TYPE_CHECKING:
    from prefrontal.memory.store import MemoryStore

#: How many repeat-sender hints / quick-drop examples to inject at most. Bounded
#: so the addendum stays small and can't push the model toward over-suppressing.
_SENDER_LIMIT = 8
_EXAMPLE_LIMIT = 6

#: Upper bound on the hard denylist. Far larger than the prompt-hint limit: the
#: denylist is a deterministic gate (not text fed to a model), so it costs
#: nothing to carry every repeat-offender and we don't want to silently truncate.
_DENYLIST_LIMIT = 500


def record_drop_feedback(
    store: MemoryStore,
    todo_id: int,
    todo: dict[str, Any],
    *,
    now: datetime | None = None,
) -> int | None:
    """Record a triage correction iff ``todo`` was created by mail intake.

    Looks up the email that spawned ``todo`` (via ``mail_messages.todo_id``); if
    there is none, the todo was manual/impulse and this is a no-op (returns
    ``None``). Otherwise stores the drop with the originating email's context and
    the todo's age, and returns the new ``triage_feedback`` row id.

    Args:
        store: A user-scoped :class:`~prefrontal.memory.store.MemoryStore`.
        todo_id: The dropped todo's id.
        todo: The dropped todo dict (post-close), for its ``created_at``.
        now: Reference time (naive UTC) for computing how long the todo was open.
    """
    mail = store.mail_by_todo(todo_id)
    if mail is None:
        return None

    created = _parse_ts(todo.get("created_at"))
    days_open: float | None = None
    if created is not None and now is not None:
        days_open = max(0.0, (now - created).total_seconds() / 86400.0)

    return store.record_triage_drop(
        todo_id=todo_id,
        message_id=mail.get("message_id"),
        sender_email=mail.get("sender_email"),
        sender_name=mail.get("sender_name"),
        subject=mail.get("subject"),
        summary=mail.get("summary"),
        category=mail.get("category"),
        urgency=mail.get("urgency"),
        days_open=days_open,
    )


def learned_corrections(
    store: MemoryStore,
    *,
    quick_drop_days: float = 2.0,
    repeat_threshold: int = 2,
    sender_limit: int = _SENDER_LIMIT,
    example_limit: int = _EXAMPLE_LIMIT,
) -> str:
    """Build the triage system-prompt addendum from this user's drop history.

    Combines the two reliable signals — repeat-offender senders and recent quick
    drops — into the block that :func:`prefrontal.mail.triage.triage_message`
    appends to its system prompt. Returns ``""`` when nothing qualifies, so a
    user with no (or only ambiguous) drops gets the unmodified base prompt.
    """
    senders = store.triage_dropped_senders(
        min_count=repeat_threshold, limit=sender_limit
    )
    quick = store.triage_recent_quick_drops(
        max_days=quick_drop_days, limit=example_limit
    )
    examples = [
        {
            "sender": q.get("sender_name") or q.get("sender_email") or "unknown",
            "subject": q.get("subject"),
            "summary": q.get("summary"),
        }
        for q in quick
    ]
    return build_corrections_block(senders, examples)


def learned_denylist(
    store: MemoryStore,
    *,
    repeat_threshold: int = 2,
    limit: int = _DENYLIST_LIMIT,
) -> frozenset[str]:
    """Lowercased sender emails to hard-suppress from todo creation.

    The deterministic counterpart to :func:`learned_corrections`: the same
    repeat-dropped senders (:meth:`MemoryStore.triage_dropped_senders`), but
    returned as an exact-match set for :func:`prefrontal.mail.triage.
    suppress_todo_reason` to gate on — so a sender you keep dropping stops
    producing todos regardless of what the (over-eager) model says, instead of
    merely being *hinted* at in the prompt. Returns an empty set when no sender
    has reached ``repeat_threshold`` drops.
    """
    return frozenset(
        (s["sender_email"] or "").strip().lower()
        for s in store.triage_dropped_senders(min_count=repeat_threshold, limit=limit)
        if s.get("sender_email")
    )
