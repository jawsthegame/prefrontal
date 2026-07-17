"""People roster + the mention review queue.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone. Two
tables, one feature (see :mod:`prefrontal.people`):

- ``people`` — the identified, categorized roster. Matched case-insensitively by
  ``name_key`` (and by ``aliases``), carrying a ``relationship`` and an
  ``importance`` that steer learning and prioritization, plus the
  ``mention_count``/``last_seen`` recurrence signal.
- ``person_mentions`` — the review queue. A name pulled from an ingested item
  that isn't yet on the roster lands here **pending** until the user identifies it
  (links a person) or dismisses it. Like a sensor proposal, nothing authoritative
  is written from a raw extracted name.
"""
from __future__ import annotations

import json
from typing import Any

from prefrontal.memory.repos._base import Repo

#: Columns selected for a person row (kept in one place so every read matches).
_PERSON_COLS = (
    "id, name, name_key, relationship, importance, aliases, notes, "
    "mention_count, first_seen, last_seen, status, created_at"
)
#: Columns selected for a mention row.
_MENTION_COLS = (
    "id, name, name_key, source, context, ref, external_id, person_id, "
    "status, created_at, resolved_at"
)


class PeopleRepo(Repo):
    """Reads/writes for the people roster and the mention review queue."""

    # --- roster --------------------------------------------------------------

    def add_person(
        self,
        *,
        name: str,
        name_key: str,
        relationship: str = "unknown",
        importance: int = 1,
        aliases: list[str] | None = None,
        notes: str | None = None,
        mention_count: int = 0,
        seen_at: str | None = None,
        status: str = "active",
    ) -> int:
        """Insert a roster person and return its id.

        ``seen_at`` (when the naming item was ingested) seeds ``first_seen`` /
        ``last_seen`` and, when ``mention_count`` is 0 but a sighting is supplied,
        counts as the first mention (so a populated ``first_seen`` never sits
        beside a zero count). A duplicate ``name_key`` for this user raises (the
        caller should :meth:`find_person` first); the roster is human-curated, so
        a clash is a real "already exists" the surface reports.
        """
        # A supplied sighting is itself the first mention — keep the count and the
        # first/last_seen stamps consistent.
        count = mention_count or (1 if seen_at else 0)
        cur = self.conn.execute(
            "INSERT INTO people "
            "(user_id, name, name_key, relationship, importance, aliases, notes, "
            "mention_count, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._uid(),
                name,
                name_key,
                relationship,
                importance,
                json.dumps(aliases or []),
                notes,
                count,
                seen_at,
                seen_at,
                status,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        """Return one of this user's people by id, or ``None``."""
        row = self.conn.execute(
            f"SELECT {_PERSON_COLS} FROM people WHERE user_id = ? AND id = ?",
            (self._uid(), person_id),
        ).fetchone()
        return self._person_row(row) if row is not None else None

    def find_person(self, key: str) -> dict[str, Any] | None:
        """Return the person matching ``key`` by ``name_key`` or an alias, or ``None``.

        The exact ``name_key`` is tried first (indexed by the unique constraint);
        failing that, active people are scanned for ``key`` among their aliases.
        The roster is small and human-paced, so the alias scan is cheap.
        """
        row = self.conn.execute(
            f"SELECT {_PERSON_COLS} FROM people WHERE user_id = ? AND name_key = ?",
            (self._uid(), key),
        ).fetchone()
        if row is not None:
            return self._person_row(row)
        for candidate in self.list_people(status="active"):
            if key in {str(a).lower() for a in candidate.get("aliases", [])}:
                return candidate
        return None

    def list_people(self, status: str = "active", limit: int = 500) -> list[dict[str, Any]]:
        """Return this user's people, most-mentioned then newest first.

        Args:
            status: Filter to this status (``active``/``archived``), or ``""`` for
                any status.
            limit: Maximum rows to return.
        """
        rows = self.conn.execute(
            f"SELECT {_PERSON_COLS} FROM people "
            "WHERE user_id = ? AND (? = '' OR status = ?) "
            "ORDER BY mention_count DESC, id DESC LIMIT ?",
            (self._uid(), status, status, limit),
        ).fetchall()
        return [self._person_row(r) for r in rows]

    def update_person(self, person_id: int, **fields: Any) -> dict[str, Any] | None:
        """Update an allowlisted subset of a person's fields; return the fresh row.

        Accepts ``name``, ``name_key``, ``relationship``, ``importance``,
        ``aliases`` (a list, stored as JSON), ``notes``, and ``status``. Unknown
        keys are ignored. Returns ``None`` if the person doesn't exist.
        """
        if self.get_person(person_id) is None:
            return None
        allowed = {
            "name", "name_key", "relationship", "importance", "aliases", "notes", "status",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            params.append(json.dumps(value) if key == "aliases" else value)
        if sets:
            params.extend([self._uid(), person_id])
            self.conn.execute(
                f"UPDATE people SET {', '.join(sets)} WHERE user_id = ? AND id = ?",
                tuple(params),
            )
            self.conn.commit()
        return self.get_person(person_id)

    def touch_person(self, person_id: int, *, seen_at: str | None = None) -> None:
        """Record that an ingested item named this person (learning signal).

        Bumps ``mention_count`` and advances ``last_seen`` (and back-fills
        ``first_seen`` if unset). ``seen_at`` defaults to the current time.
        """
        if seen_at:
            self.conn.execute(
                "UPDATE people SET mention_count = mention_count + 1, "
                "last_seen = ?, first_seen = COALESCE(first_seen, ?) "
                "WHERE user_id = ? AND id = ?",
                (seen_at, seen_at, self._uid(), person_id),
            )
        else:
            self.conn.execute(
                "UPDATE people SET mention_count = mention_count + 1, "
                "last_seen = datetime('now'), "
                "first_seen = COALESCE(first_seen, datetime('now')) "
                "WHERE user_id = ? AND id = ?",
                (self._uid(), person_id),
            )
        self.conn.commit()

    # --- mention queue -------------------------------------------------------

    def add_person_mention(
        self,
        *,
        name: str,
        source: str = "triage",
        context: str | None = None,
        ref: str | None = None,
        external_id: str | None = None,
    ) -> int:
        """Queue a **pending** mention for review; return its id (0 if de-duped).

        De-dupes on the normalized name via the partial unique index: if a pending
        mention for the same name already exists, this is a no-op returning ``0``
        (the recurrence is captured by touching the person / leaving the one queue
        row). ``name_key`` is derived here so callers pass the name as it appeared.
        """
        from prefrontal.people import name_key as _name_key

        key = _name_key(name)
        cur = self.conn.execute(
            "INSERT INTO person_mentions "
            "(user_id, name, name_key, source, context, ref, external_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, name_key) WHERE status = 'pending' DO NOTHING",
            (self._uid(), name, key, source, context, ref, external_id),
        )
        self.conn.commit()
        return int(cur.lastrowid) if cur.rowcount > 0 else 0

    def get_person_mention(self, mention_id: int) -> dict[str, Any] | None:
        """Return one of this user's mentions by id, or ``None``."""
        row = self.conn.execute(
            f"SELECT {_MENTION_COLS} FROM person_mentions WHERE user_id = ? AND id = ?",
            (self._uid(), mention_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_person_mentions(
        self, status: str = "pending", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return this user's mentions (newest first).

        Args:
            status: Filter to this status (``pending``/``identified``/``dismissed``),
                or ``""`` for any status.
            limit: Maximum rows to return.
        """
        rows = self.conn.execute(
            f"SELECT {_MENTION_COLS} FROM person_mentions "
            "WHERE user_id = ? AND (? = '' OR status = ?) "
            "ORDER BY id DESC LIMIT ?",
            (self._uid(), status, status, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_pending_mentions(self) -> int:
        """How many mentions are awaiting review (for a badge / summary line)."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM person_mentions WHERE user_id = ? AND status = 'pending'",
            (self._uid(),),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def identify_person_mention(self, mention_id: int, person_id: int) -> bool:
        """Resolve a *pending* mention to ``identified``, linking ``person_id``.

        Only a pending row moves (so a double-identify is a no-op), keeping the
        resolution idempotent. Returns ``True`` if a row was updated.
        """
        cur = self.conn.execute(
            "UPDATE person_mentions SET status = 'identified', person_id = ?, "
            "resolved_at = datetime('now') "
            "WHERE user_id = ? AND id = ? AND status = 'pending'",
            (person_id, self._uid(), mention_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def dismiss_person_mention(self, mention_id: int) -> bool:
        """Dismiss a *pending* mention ("not a person / not worth tracking").

        Idempotent — only a pending row moves. Returns ``True`` if a row was
        updated.
        """
        cur = self.conn.execute(
            "UPDATE person_mentions SET status = 'dismissed', resolved_at = datetime('now') "
            "WHERE user_id = ? AND id = ? AND status = 'pending'",
            (self._uid(), mention_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def has_dismissed_mention(self, name_key: str) -> bool:
        """Whether this user ever dismissed a mention of this normalized name.

        A dismissal is a durable "not a person / don't surface" signal. The
        pending unique index only de-dupes *pending* rows, so without consulting
        past dismissals a recurring false positive ("Order Confirmation", "Field
        Trip") re-queues on every later appearance and the review queue never
        converges. :func:`prefrontal.people.enqueue_mentions` checks this before
        queuing. (Identifying a mention instead creates a ``people`` row, which
        that path already short-circuits on.)
        """
        row = self.conn.execute(
            "SELECT 1 FROM person_mentions "
            "WHERE user_id = ? AND name_key = ? AND status = 'dismissed' LIMIT 1",
            (self._uid(), name_key),
        ).fetchone()
        return row is not None

    @staticmethod
    def _person_row(row: Any) -> dict[str, Any]:
        d = dict(row)
        try:
            aliases = json.loads(d["aliases"]) if d.get("aliases") else []
        except (ValueError, TypeError):
            aliases = []
        # Coerce any non-list payload (e.g. a stray JSON null/object) to [] so
        # callers that iterate aliases never crash on a malformed row.
        d["aliases"] = aliases if isinstance(aliases, list) else []
        return d
