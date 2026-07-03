"""Ingested/triaged mail and the triage-feedback learning signal.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
    sql_placeholders,
)
from prefrontal.memory.repos._base import Repo


class MailRepo(Repo):
    """Ingested/triaged mail and the triage-feedback learning signal."""

    def seen_mail_ids(self, account: str | None = None) -> set[str]:
        """Return the ``message_id``\\ s already ingested, for dedup.

        Args:
            account: If given, scope to one account's messages; otherwise return
                ids across all accounts. Dedup is account-scoped (the unique
                constraint is on ``(account, message_id)``), so callers ingesting
                one account should pass it.

        Returns:
            A set of ``message_id`` strings.
        """
        if account is None:
            rows = self.conn.execute(
                "SELECT message_id FROM mail_messages WHERE user_id = ?",
                (self._uid(),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT message_id FROM mail_messages WHERE user_id = ? AND account = ?",
                (self._uid(), account),
            ).fetchall()
        return {r["message_id"] for r in rows}

    def record_mail(
        self,
        *,
        account: str,
        message_id: str,
        policy: str = "full",
        thread_id: str | None = None,
        sender_name: str | None = None,
        sender_email: str | None = None,
        subject: str | None = None,
        received_at: str | None = None,
        snippet: str | None = None,
        body: str | None = None,
        unread: bool | None = None,
        needs_action: bool = False,
        urgency: str | None = None,
        category: str | None = None,
        waiting_on: str | None = None,
        summary: str | None = None,
        triage_source: str | None = None,
        todo_id: int | None = None,
    ) -> int:
        """Insert a triaged message and return its row id.

        Dedup is the caller's responsibility (see :meth:`seen_mail_ids`); a
        duplicate ``(account, message_id)`` raises ``sqlite3.IntegrityError``.
        Body/snippet should already have been dropped for a ``signals`` account
        by the normalizer — this method stores exactly what it is given.

        Returns:
            The new ``mail_messages`` row id.
        """
        cur = self.conn.execute(
            "INSERT INTO mail_messages ("
            "user_id, account, message_id, thread_id, sender_name, sender_email, "
            "subject, received_at, snippet, body, unread, needs_action, urgency, "
            "category, waiting_on, summary, triage_source, policy, todo_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._uid(), account, message_id, thread_id, sender_name,
                sender_email, subject, received_at, snippet, body, unread,
                needs_action, urgency, category, waiting_on, summary,
                triage_source, policy, todo_id,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_mail(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recently ingested messages, newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of ``mail_messages`` dicts ordered by ``received_at`` (then
            ``id``) descending.
        """
        rows = self.conn.execute(
            "SELECT * FROM mail_messages WHERE user_id = ? "
            "ORDER BY (received_at IS NULL), received_at DESC, id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mail_needing_action(self) -> list[dict[str, Any]]:
        """Return ingested messages still flagged ``needs_action``, newest first.

        A message stays here until the linked todo is closed; messages whose
        ``todo_id`` todo is no longer open are excluded, so resolving the open
        loop clears the mail from the action list.

        Returns:
            A list of ``mail_messages`` dicts.
        """
        rows = self.conn.execute(
            "SELECT m.* FROM mail_messages m "
            "LEFT JOIN todos t ON m.todo_id = t.id "
            "WHERE m.user_id = ? AND m.needs_action = 1 "
            "AND (m.todo_id IS NULL OR t.status = 'open') "
            "ORDER BY (m.received_at IS NULL), m.received_at DESC, m.id DESC",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def mail_by_todo(self, todo_id: int) -> dict[str, Any] | None:
        """Return the mail message that created ``todo_id``, or ``None``.

        The reverse of the ``mail_messages.todo_id`` link: given a todo, recover
        the email that spawned it. Used when a todo is dropped, to tell whether
        it was an intake todo (and so a triage correction) versus a manual one.
        """
        row = self.conn.execute(
            "SELECT * FROM mail_messages WHERE todo_id = ? AND user_id = ?",
            (todo_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def mail_for_retriage(
        self, *, account: str | None = None, only_needs_action: bool = True
    ) -> list[dict[str, Any]]:
        """Return stored messages to re-run triage over, oldest first.

        The re-triage counterpart to :meth:`seen_mail_ids`: rather than dedup new
        arrivals, this hands back the rows already stored so the current prompt
        can re-classify them (e.g. after the triage prompt evolved).

        Args:
            account: If given, scope to one account; otherwise every account.
            only_needs_action: When ``True`` (default), return only messages
                currently flagged ``needs_action`` — the ones cluttering the
                action list. When ``False``, return every stored message (a full
                re-triage that can also newly flag previously-cleared mail).

        Returns:
            A list of full ``mail_messages`` dicts (body included) in id order.
        """
        clauses = ["user_id = ?"]
        params: list[Any] = [self._uid()]
        if account is not None:
            clauses.append("account = ?")
            params.append(account)
        if only_needs_action:
            clauses.append("needs_action = 1")
        rows = self.conn.execute(
            "SELECT * FROM mail_messages WHERE " + " AND ".join(clauses) + " ORDER BY id ASC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def update_mail_triage(
        self,
        mail_id: int,
        *,
        needs_action: bool,
        urgency: str | None,
        category: str | None,
        waiting_on: str | None,
        summary: str | None,
        triage_source: str | None,
        todo_id: int | None,
    ) -> bool:
        """Overwrite a stored message's triage verdict. Returns ``True`` if changed.

        Used by re-triage to fold a fresh verdict back onto an existing row. The
        message's identity/content columns (sender, subject, body, …) are left
        untouched — only the derived verdict and its ``todo_id`` link move.

        Args:
            mail_id: The ``mail_messages`` row id to update.
            todo_id: The linked todo after reconciliation (unchanged for cleared
                items, a new id for newly-flagged ones, or ``None``).
        """
        cur = self.conn.execute(
            "UPDATE mail_messages SET needs_action = ?, urgency = ?, category = ?, "
            "waiting_on = ?, summary = ?, triage_source = ?, todo_id = ? "
            "WHERE id = ? AND user_id = ?",
            (
                needs_action, urgency, category, waiting_on, summary,
                triage_source, todo_id, mail_id, self._uid(),
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def mail_accounts_for_todos(self, todo_ids: list[int]) -> dict[int, str]:
        """Map each todo id to the mail account that created it (batch).

        The account is authoritative via the ``mail_messages.todo_id`` link — the
        same link :meth:`mail_by_todo` uses — so a surface can label a todo with
        the inbox it came from without re-parsing the todo's notes. Todos with no
        originating mail (manual/impulse) are simply absent from the result.

        Args:
            todo_ids: The todo ids to look up (empty is fine).

        Returns:
            A ``{todo_id: account}`` dict covering only mail-created todos.
        """
        if not todo_ids:
            return {}
        placeholders = sql_placeholders(len(todo_ids))
        rows = self.conn.execute(
            f"SELECT todo_id, account FROM mail_messages "
            f"WHERE user_id = ? AND todo_id IN ({placeholders})",
            (self._uid(), *todo_ids),
        ).fetchall()
        return {r["todo_id"]: r["account"] for r in rows if r["todo_id"] is not None}

    def mail_sources_for_todos(self, todo_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Map each todo id to its originating mail's ``account``/ids (batch).

        The richer companion to :meth:`mail_accounts_for_todos`: alongside the
        account it also returns the provider ``message_id`` and ``thread_id``,
        which a surface turns into a deep link back to the source email (e.g. a
        Gmail ``rfc822msgid:`` search). Uses the same ``mail_messages.todo_id``
        link, so todos with no originating mail are simply absent.

        Args:
            todo_ids: The todo ids to look up (empty is fine).

        Returns:
            A ``{todo_id: {"account", "message_id", "thread_id"}}`` dict covering
            only mail-created todos.
        """
        if not todo_ids:
            return {}
        placeholders = sql_placeholders(len(todo_ids))
        rows = self.conn.execute(
            f"SELECT todo_id, account, message_id, thread_id FROM mail_messages "
            f"WHERE user_id = ? AND todo_id IN ({placeholders})",
            (self._uid(), *todo_ids),
        ).fetchall()
        return {
            r["todo_id"]: {
                "account": r["account"],
                "message_id": r["message_id"],
                "thread_id": r["thread_id"],
            }
            for r in rows
            if r["todo_id"] is not None
        }

    def record_triage_drop(
        self,
        *,
        todo_id: int | None,
        message_id: str | None,
        sender_email: str | None,
        sender_name: str | None,
        subject: str | None,
        summary: str | None,
        category: str | None,
        urgency: str | None,
        days_open: float | None,
    ) -> int:
        """Record that the user dropped an intake-created todo (one row per drop).

        Stores the originating email's context — sender, subject, the triage
        verdict it got, and how long the todo sat open before being dropped — so
        :func:`prefrontal.mail.feedback.learned_corrections` can later separate a
        genuine false-positive (quick or repeated) from an avoidance drop.

        Returns:
            The new ``triage_feedback`` row id.
        """
        cur = self.conn.execute(
            "INSERT INTO triage_feedback ("
            "user_id, todo_id, message_id, sender_email, sender_name, subject, "
            "summary, category, urgency, days_open"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._uid(), todo_id, message_id, sender_email, sender_name,
                subject, summary, category, urgency, days_open,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def triage_dropped_senders(
        self, *, min_count: int = 2, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Senders whose intake todos the user has dropped ``min_count``+ times.

        Repetition is the reliable signal: dropping mail from the same sender
        again and again means that sender's mail rarely needs action (a real
        person you keep ignoring is rare — it's almost always a semi-automated
        sender that slipped past triage). Returned most-dropped first.
        """
        rows = self.conn.execute(
            "SELECT sender_email, MAX(sender_name) AS sender_name, "
            "COUNT(*) AS drops FROM triage_feedback "
            "WHERE user_id = ? AND sender_email IS NOT NULL AND sender_email != '' "
            "GROUP BY sender_email HAVING COUNT(*) >= ? "
            "ORDER BY drops DESC, MAX(created_at) DESC LIMIT ?",
            (self._uid(), min_count, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def triage_recent_quick_drops(
        self, *, max_days: float = 2.0, limit: int = 6
    ) -> list[dict[str, Any]]:
        """Recent drops that happened *quickly* after the todo was created.

        A todo dropped soon after it arrived (before it had time to be avoided)
        is the cleanest single-occurrence false-positive signal. Drops with an
        unknown age are excluded (we can't tell quick from avoided). Newest first.
        """
        rows = self.conn.execute(
            "SELECT sender_email, sender_name, subject, summary, category, urgency "
            "FROM triage_feedback "
            "WHERE user_id = ? AND days_open IS NOT NULL AND days_open <= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (self._uid(), max_days, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def triage_feedback_list(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recorded drop corrections, newest first (for inspection/curation)."""
        rows = self.conn.execute(
            "SELECT id, todo_id, sender_email, sender_name, subject, summary, "
            "category, urgency, days_open, created_at FROM triage_feedback "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def forget_triage_feedback(self, feedback_id: int) -> bool:
        """Delete one drop correction. Returns ``True`` if a row was removed."""
        cur = self.conn.execute(
            "DELETE FROM triage_feedback WHERE id = ? AND user_id = ?",
            (feedback_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def clear_triage_feedback(self) -> int:
        """Delete all of this user's drop corrections. Returns how many were removed."""
        cur = self.conn.execute(
            "DELETE FROM triage_feedback WHERE user_id = ?",
            (self._uid(),),
        )
        self.conn.commit()
        return cur.rowcount
