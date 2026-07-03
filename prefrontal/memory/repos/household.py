"""Shared household sheet — facts, agreements, roster (household-scoped).

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.

Where the rest of the store scopes every row to one ``user_id`` (see
:meth:`MemoryStore._uid`), these tables scope to a **household**: two co-parents
belong to the same household and see the *same* rows. This is the deliberate
exception to strict per-user isolation — the whole point of the shared sheet.
The scope guard is the mirror of ``_uid()``: :meth:`_household_id` resolves the
caller's household from their user row and every statement injects
``WHERE household_id = ?``, so no call site can forget it and cross households.

A user with **no** household calling a household read/write gets a loud
``RuntimeError`` (like the unscoped-store guard) rather than a silent empty read.

See ``docs/household-sheet.md`` for the full design.
"""
from __future__ import annotations

import re
import secrets
from typing import Any

from prefrontal.memory._helpers import _row_to_dict
from prefrontal.memory.repos._base import Repo

#: Invite-code alphabet — uppercase + digits minus ambiguous glyphs (no O/0/I/1),
#: so a code read off one phone and typed into another is unambiguous.
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: The controlled vocabulary for ``household_facts.category`` — a small, fixed set
#: mirroring ``todos.KNOWN_CATEGORIES``' single-source-of-truth pattern, so the
#: assistant validates against it and the render groups by it in a stable order.
FACT_CATEGORIES: tuple[str, ...] = (
    "sizes",    # tops / bottoms / shoes + brand notes
    "routine",  # wake / breakfast / ready-by / bedtime
    "food",     # allergies + severity + EpiPen, likes/dislikes
    "health",   # pediatrician / dentist / insurance / meds
    "school",   # teacher / room / activities / pickup
    "contact",  # role -> name / phone (a facts facet; see docs §3.7)
)

#: Human labels for the fact categories, used by the render's section headings.
FACT_CATEGORY_LABELS: dict[str, str] = {
    "sizes": "Clothing & sizes",
    "routine": "Routines",
    "food": "Food & allergies",
    "health": "Health",
    "school": "School & activities",
    "contact": "Key contacts",
}

#: Allowed ``household_agreements.kind`` values.
AGREEMENT_KINDS: tuple[str, ...] = ("reward", "consistency", "routine")

#: The sentinel ``child_id`` for a household-wide (not per-child) fact/agreement.
#: A real child is a positive ``children.id``; 0 keeps household-wide rows distinct
#: under the UNIQUE constraint (SQLite treats NULLs as distinct — see the schema).
HOUSEHOLD_WIDE = 0


def normalize_fact_category(value: str | None) -> str | None:
    """Return a valid :data:`FACT_CATEGORIES` member, or ``None`` if unrecognized.

    Lowercases/trims and accepts only a known category, so an out-of-vocabulary
    string (from a model or an API caller) is rejected rather than stored as a
    one-off category that no render section would ever show.
    """
    if not isinstance(value, str):
        return None
    norm = re.sub(r"\s+", " ", value.strip().lower())
    return norm if norm in FACT_CATEGORIES else None


def normalize_fact_item(value: str | None) -> str:
    """Canonicalize a fact ``item`` label: lowercased, single-spaced, length-capped.

    Mirrors ``todos.normalize_category`` so the composite upsert key
    (household, child, category, item) is stable across "Shoe Size" / "shoe size".
    """
    return re.sub(r"\s+", " ", (value or "").strip().lower())[:60]


class HouseholdRepo(Repo):
    """Shared household sheet — facts, agreements, roster (household-scoped)."""

    # -- scope resolution -----------------------------------------------------

    def _household_id(self) -> int:
        """The caller's household id, or raise (the mirror of :meth:`_uid`).

        Reads ``household_id`` off the bound user's row. Raises if the store is
        unscoped (``_uid()`` raises) or the user is in no household — never a
        silent empty read across every household's rows.
        """
        hid = self.household_id_or_none()
        if hid is None:
            raise RuntimeError(
                "store's user is not in a household — an operator must run "
                "set_user_household() first"
            )
        return hid

    def household_id_or_none(self) -> int | None:
        """The caller's household id, or ``None`` if they are in no household.

        The non-raising form used by callers (e.g. the assistant snapshot) that
        must degrade gracefully for a user who isn't a co-parent.
        """
        row = self.conn.execute(
            "SELECT household_id FROM users WHERE id = ?", (self._uid(),)
        ).fetchone()
        return row["household_id"] if row is not None else None

    def household(self) -> dict[str, Any] | None:
        """The caller's household row (id/name/created_at), or ``None``."""
        hid = self.household_id_or_none()
        if hid is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM households WHERE id = ?", (hid,)
        ).fetchone()
        return _row_to_dict(row)

    def household_member_count(self) -> int:
        """How many active members the caller's household has (0 if in none).

        The single-parent switch: features that only make sense *between* two
        co-parents (the mental-load check-in, the delta digest) gate on this being
        >= 2, so a household of one silently skips them — and they light up on
        their own the moment a second parent joins.
        """
        hid = self.household_id_or_none()
        if hid is None:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE household_id = ? AND status = 'active'",
            (hid,),
        ).fetchone()
        return int(row["n"])

    def is_shared_household(self) -> bool:
        """Whether the caller co-parents with someone (>= 2 active members)."""
        return self.household_member_count() >= 2

    # -- roster ---------------------------------------------------------------

    def add_child(self, *, name: str, birthday: str | None = None) -> int:
        """Add a child to the household roster (idempotent on name), returning its id.

        Re-adding an existing name updates only a newly-supplied birthday and
        returns the same id — so "add Sam" twice never creates two Sams.
        """
        hid = self._household_id()
        self.conn.execute(
            """
            INSERT INTO children (household_id, name, birthday)
            VALUES (?, ?, ?)
            ON CONFLICT (household_id, name) DO UPDATE SET
                birthday = COALESCE(excluded.birthday, children.birthday)
            """,
            (hid, name.strip(), birthday),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM children WHERE household_id = ? AND name = ?",
            (hid, name.strip()),
        ).fetchone()
        return int(row["id"])

    def rename_child(self, child_id: int, *, name: str, birthday: str | None = None) -> bool:
        """Rename a child (and optionally set a birthday). ``True`` if a row changed.

        Scoped to the household so one household can't rename another's kid. A
        ``birthday`` of ``None`` leaves the stored birthday untouched.
        """
        if birthday is None:
            cur = self.conn.execute(
                "UPDATE children SET name = ? WHERE id = ? AND household_id = ?",
                (name.strip(), child_id, self._household_id()),
            )
        else:
            cur = self.conn.execute(
                "UPDATE children SET name = ?, birthday = ? "
                "WHERE id = ? AND household_id = ?",
                (name.strip(), birthday, child_id, self._household_id()),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def children(self) -> list[dict[str, Any]]:
        """The household's children (id/name/birthday), ordered by name."""
        rows = self.conn.execute(
            "SELECT id, name, birthday, created_at FROM children "
            "WHERE household_id = ? ORDER BY name ASC",
            (self._household_id(),),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- facts ----------------------------------------------------------------

    def set_fact(
        self,
        *,
        category: str,
        item: str,
        value: str | None,
        updated_by: int | None,
        child_id: int = HOUSEHOLD_WIDE,
    ) -> None:
        """Upsert one per-kid (or household-wide) fact, stamping provenance.

        Same upsert shape as :meth:`set_state`, on the household scope: the
        composite key is (household, child, category, item), and every write
        records ``updated_by``/``updated_at`` — the raw material for the load
        digest. ``updated_by`` is the *acting* user, never model-supplied.
        """
        self.conn.execute(
            """
            INSERT INTO household_facts
                (household_id, child_id, category, item, value, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (household_id, child_id, category, item) DO UPDATE SET
                value      = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (self._household_id(), child_id, category, item, value, updated_by),
        )
        self.conn.commit()

    def clear_fact(
        self, *, category: str, item: str, child_id: int = HOUSEHOLD_WIDE
    ) -> bool:
        """Delete one fact. ``True`` if a row was removed."""
        cur = self.conn.execute(
            "DELETE FROM household_facts WHERE household_id = ? AND child_id = ? "
            "AND category = ? AND item = ?",
            (self._household_id(), child_id, category, item),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def facts(self) -> list[dict[str, Any]]:
        """All household facts, each with the child's name and who last set it.

        Joins ``children`` (for the child name; ``NULL`` for a household-wide
        ``child_id = 0`` row) and ``users`` (for the updater's display name), so
        the render never has to re-resolve ids. Ordered child-then-category-then-
        item for a stable grid.
        """
        rows = self.conn.execute(
            """
            SELECT f.id, f.child_id, f.category, f.item, f.value,
                   f.updated_by, f.updated_at,
                   c.name AS child_name,
                   COALESCE(u.display_name, u.handle) AS updated_by_name
            FROM household_facts f
            LEFT JOIN children c
                   ON c.id = f.child_id AND c.household_id = f.household_id
            LEFT JOIN users u ON u.id = f.updated_by
            WHERE f.household_id = ?
            ORDER BY f.child_id ASC, f.category ASC, f.item ASC
            """,
            (self._household_id(),),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- agreements -----------------------------------------------------------

    def set_agreement(
        self,
        *,
        title: str,
        body: str | None,
        updated_by: int | None,
        kind: str = "consistency",
        structured: str | None = None,
        child_id: int = HOUSEHOLD_WIDE,
    ) -> int:
        """Upsert a standing behaviour plan (keyed on child+title), returning its id."""
        hid = self._household_id()
        self.conn.execute(
            """
            INSERT INTO household_agreements
                (household_id, child_id, title, kind, body, structured, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (household_id, child_id, title) DO UPDATE SET
                kind       = excluded.kind,
                body       = excluded.body,
                structured = excluded.structured,
                updated_by = excluded.updated_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (hid, child_id, title.strip(), kind, body, structured, updated_by),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM household_agreements "
            "WHERE household_id = ? AND child_id = ? AND title = ?",
            (hid, child_id, title.strip()),
        ).fetchone()
        return int(row["id"])

    def remove_agreement(self, agreement_id: int) -> bool:
        """Delete an agreement by id (scoped to the household). ``True`` if removed."""
        cur = self.conn.execute(
            "DELETE FROM household_agreements WHERE id = ? AND household_id = ?",
            (agreement_id, self._household_id()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def agreements(self) -> list[dict[str, Any]]:
        """All standing agreements, with child name and updater, newest-touched first."""
        rows = self.conn.execute(
            """
            SELECT a.id, a.child_id, a.title, a.kind, a.body, a.structured,
                   a.updated_by, a.updated_at, a.last_prompted_at,
                   c.name AS child_name,
                   COALESCE(u.display_name, u.handle) AS updated_by_name
            FROM household_agreements a
            LEFT JOIN children c
                   ON c.id = a.child_id AND c.household_id = a.household_id
            LEFT JOIN users u ON u.id = a.updated_by
            WHERE a.household_id = ?
            ORDER BY a.updated_at DESC, a.id DESC
            """,
            (self._household_id(),),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- star chart tracking --------------------------------------------------
    #
    # An agreement's `structured` JSON declares the goals (thresholds -> rewards);
    # household_stars is the running earnings. Awarding is a plain ledger insert;
    # the goal-crossing math lives in prefrontal.household (pure, testable), fed
    # by the before/after totals award_stars() returns.

    def agreement(self, agreement_id: int) -> dict[str, Any] | None:
        """One agreement row (scoped to the household), or ``None`` if not found.

        Used by the star-award path to read the chart's ``structured`` goals and
        ``child_id`` before recording a grant — so a caller can't award against
        another household's agreement (it simply reads back ``None`` → 404).
        """
        row = self.conn.execute(
            "SELECT id, child_id, title, kind, body, structured, last_prompted_at "
            "FROM household_agreements WHERE id = ? AND household_id = ?",
            (agreement_id, self._household_id()),
        ).fetchone()
        return _row_to_dict(row)

    def mark_prompted(self, agreement_id: int) -> bool:
        """Stamp a chart's ``last_prompted_at`` = now (dedups the daily award prompt)."""
        cur = self.conn.execute(
            "UPDATE household_agreements SET last_prompted_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND household_id = ?",
            (agreement_id, self._household_id()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def award_stars(
        self,
        *,
        agreement_id: int,
        delta: int,
        awarded_by: int | None,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        """Record a star grant against an agreement's chart; return before/after totals.

        Returns ``None`` if the agreement isn't in this household (so the caller
        can 404 rather than writing an orphan ledger row). ``child_id`` is copied
        from the agreement — never passed in — so the ledger always agrees with
        the chart it belongs to. The running total is derived (``SUM(delta)``),
        so a grant is append-only and every award keeps its own provenance.
        """
        hid = self._household_id()
        agr = self.conn.execute(
            "SELECT child_id FROM household_agreements WHERE id = ? AND household_id = ?",
            (agreement_id, hid),
        ).fetchone()
        if agr is None:
            return None
        before = self.star_total(agreement_id)
        self.conn.execute(
            """
            INSERT INTO household_stars
                (household_id, agreement_id, child_id, delta, note, awarded_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (hid, agreement_id, agr["child_id"], int(delta), note, awarded_by),
        )
        self.conn.commit()
        return {
            "agreement_id": agreement_id,
            "child_id": agr["child_id"],
            "delta": int(delta),
            "before": before,
            "after": before + int(delta),
        }

    def star_total(self, agreement_id: int) -> int:
        """The running star total for one chart (``SUM(delta)``, 0 if none)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(delta), 0) AS total FROM household_stars "
            "WHERE household_id = ? AND agreement_id = ?",
            (self._household_id(), agreement_id),
        ).fetchone()
        return int(row["total"])

    def star_totals(self) -> dict[int, int]:
        """Running totals for every chart in the household, keyed by ``agreement_id``.

        One grouped query so :func:`prefrontal.household.build_sheet` can show each
        chart's progress without a per-agreement round-trip.
        """
        rows = self.conn.execute(
            "SELECT agreement_id, COALESCE(SUM(delta), 0) AS total FROM household_stars "
            "WHERE household_id = ? GROUP BY agreement_id",
            (self._household_id(),),
        ).fetchall()
        return {int(r["agreement_id"]): int(r["total"]) for r in rows}

    def star_ledger(self, agreement_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
        """Recent grants for one chart (newest first), each with who awarded it."""
        rows = self.conn.execute(
            """
            SELECT s.id, s.delta, s.note, s.created_at, s.child_id,
                   COALESCE(u.display_name, u.handle) AS awarded_by_name
            FROM household_stars s
            LEFT JOIN users u ON u.id = s.awarded_by
            WHERE s.household_id = ? AND s.agreement_id = ?
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT ?
            """,
            (self._household_id(), agreement_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_star_awards(self, *, limit: int = 6) -> list[dict[str, Any]]:
        """Recent grants across all charts (newest first) for the load surface.

        Joins the chart title, child name, and awarder so the shared sheet's
        "recently changed" section can show "Sam · +2⭐ (Star chart) · Dana" —
        making a quietly-carried tracking task visible to the other parent.
        """
        rows = self.conn.execute(
            """
            SELECT s.delta, s.note, s.created_at, s.child_id, s.awarded_by,
                   a.title AS agreement_title,
                   c.name AS child_name,
                   COALESCE(u.display_name, u.handle) AS awarded_by_name
            FROM household_stars s
            JOIN household_agreements a
                  ON a.id = s.agreement_id AND a.household_id = s.household_id
            LEFT JOIN children c
                  ON c.id = s.child_id AND c.household_id = s.household_id
            LEFT JOIN users u ON u.id = s.awarded_by
            WHERE s.household_id = ?
            ORDER BY s.created_at DESC, s.id DESC
            LIMIT ?
            """,
            (self._household_id(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- weekly mental-load check-in ------------------------------------------
    #
    # Opt-in, household-scoped. The schedule (enabled/day/time) lives on the
    # household row; each parent's weekly self-report is a row in
    # household_checkins. The "is it due?" and "what's the gentle note?" logic is
    # pure (prefrontal.household); this layer just stores and reads.

    def get_checkin_config(self) -> dict[str, Any]:
        """The household's check-in schedule + last-sent stamp (all fields nullable)."""
        row = self.conn.execute(
            "SELECT checkin_enabled, checkin_day, checkin_time, checkin_last_sent_at "
            "FROM households WHERE id = ?",
            (self._household_id(),),
        ).fetchone()
        if row is None:
            return {"enabled": False, "day": None, "time": None, "last_sent_at": None}
        return {
            "enabled": bool(row["checkin_enabled"]),
            "day": row["checkin_day"],
            "time": row["checkin_time"],
            "last_sent_at": row["checkin_last_sent_at"],
        }

    def set_checkin_config(
        self, *, enabled: bool, day: int | None, time: str | None
    ) -> None:
        """Set the weekly check-in schedule (opt-in). Leaves responses untouched."""
        self.conn.execute(
            "UPDATE households SET checkin_enabled = ?, checkin_day = ?, checkin_time = ? "
            "WHERE id = ?",
            (1 if enabled else 0, day, time, self._household_id()),
        )
        self.conn.commit()

    def mark_checkin_sent(self) -> None:
        """Stamp the household's ``checkin_last_sent_at`` = now (weekly dedup)."""
        self.conn.execute(
            "UPDATE households SET checkin_last_sent_at = CURRENT_TIMESTAMP WHERE id = ?",
            (self._household_id(),),
        )
        self.conn.commit()

    def record_checkin_response(
        self, *, week: str, user_id: int | None, response: str
    ) -> None:
        """Upsert one parent's self-report for a week (re-tapping overwrites in place)."""
        self.conn.execute(
            """
            INSERT INTO household_checkins (household_id, week, user_id, response, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (household_id, week, user_id) DO UPDATE SET
                response   = excluded.response,
                created_at = CURRENT_TIMESTAMP
            """,
            (self._household_id(), week, user_id, response),
        )
        self.conn.commit()

    def checkin_responses(self, week: str) -> list[dict[str, Any]]:
        """This week's self-reports, each with the responder's display name."""
        rows = self.conn.execute(
            """
            SELECT c.user_id, c.response, c.created_at,
                   COALESCE(u.display_name, u.handle) AS by_name
            FROM household_checkins c
            LEFT JOIN users u ON u.id = c.user_id
            WHERE c.household_id = ? AND c.week = ?
            ORDER BY c.created_at ASC
            """,
            (self._household_id(), week),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- daily delta digest ---------------------------------------------------
    #
    # Opt-in household toggle; each parent's "last looked at the sheet" and "last
    # digested" stamps live in their own coaching_state (household_seen_at /
    # household_digested_at), so the digest only surfaces the *other* parent's
    # unseen changes. The diff + message are pure (prefrontal.household).

    def get_digest_enabled(self) -> bool:
        """Whether the opt-in daily delta digest is on for this household."""
        row = self.conn.execute(
            "SELECT digest_enabled FROM households WHERE id = ?",
            (self._household_id(),),
        ).fetchone()
        return bool(row["digest_enabled"]) if row is not None else False

    def set_digest_enabled(self, enabled: bool) -> None:
        """Turn the daily delta digest on or off for this household."""
        self.conn.execute(
            "UPDATE households SET digest_enabled = ? WHERE id = ?",
            (1 if enabled else 0, self._household_id()),
        )
        self.conn.commit()

    # -- load balance view ----------------------------------------------------
    #
    # A gentle, opt-in "who's been keeping the sheet up" picture from provenance
    # counts (facts.updated_by, agreements.updated_by, stars.awarded_by). Derived
    # on read; the framing lives in prefrontal.household (pure).

    def get_balance_enabled(self) -> bool:
        """Whether the opt-in load-balance view is on for this household."""
        row = self.conn.execute(
            "SELECT balance_enabled FROM households WHERE id = ?",
            (self._household_id(),),
        ).fetchone()
        return bool(row["balance_enabled"]) if row is not None else False

    def set_balance_enabled(self, enabled: bool) -> None:
        """Turn the load-balance view on or off for this household."""
        self.conn.execute(
            "UPDATE households SET balance_enabled = ? WHERE id = ?",
            (1 if enabled else 0, self._household_id()),
        )
        self.conn.commit()

    def contribution_counts(self, since: str) -> list[dict[str, Any]]:
        """Per-member "doing" counts since ``since`` (facts + agreements + stars + chores done).

        The activity facet of shared load: who actually did things — sheet edits,
        star awards, and *completed chores* (``household_chore_log.done_by``), so
        doing the dishes every night finally counts, not just editing the sheet.
        Every active member is included — even one who did nothing — so the view
        shows both parents. Sorted most-active first, then by name; a provenance
        tally, not a judgment. (The "carrying" facet is
        :meth:`accountability_counts`.)
        """
        hid = self._household_id()
        counts: dict[int, int] = {}
        queries = (
            "SELECT updated_by AS uid, COUNT(*) AS n FROM household_facts "
            "WHERE household_id = ? AND updated_at >= ? AND updated_by IS NOT NULL "
            "GROUP BY updated_by",
            "SELECT updated_by AS uid, COUNT(*) AS n FROM household_agreements "
            "WHERE household_id = ? AND updated_at >= ? AND updated_by IS NOT NULL "
            "GROUP BY updated_by",
            "SELECT awarded_by AS uid, COUNT(*) AS n FROM household_stars "
            "WHERE household_id = ? AND created_at >= ? AND awarded_by IS NOT NULL "
            "GROUP BY awarded_by",
            "SELECT done_by AS uid, COUNT(*) AS n FROM household_chore_log "
            "WHERE household_id = ? AND done_at >= ? AND done_by IS NOT NULL "
            "GROUP BY done_by",
        )
        for sql in queries:
            for r in self.conn.execute(sql, (hid, since)).fetchall():
                counts[r["uid"]] = counts.get(r["uid"], 0) + int(r["n"])
        members = self.conn.execute(
            "SELECT id, COALESCE(display_name, handle) AS name FROM users "
            "WHERE household_id = ? AND status = 'active'",
            (hid,),
        ).fetchall()
        out = [
            {"user_id": m["id"], "name": m["name"], "count": counts.get(m["id"], 0)}
            for m in members
        ]
        out.sort(key=lambda c: (-c["count"], c["name"]))
        return out

    # -- shared shopping list -------------------------------------------------
    #
    # A household-scoped checklist both parents share: add, check off (with
    # provenance on both), remove. Not per-user todos (those are user-scoped and
    # carry scheduling weight). See docs/household-sheet.md §3.7.

    def add_shopping_item(
        self,
        *,
        item: str,
        spec: str | None = None,
        where_to_buy: str | None = None,
        child_id: int = HOUSEHOLD_WIDE,
        added_by: int | None,
    ) -> int:
        """Add a thing to buy, returning its id (stamps who added it)."""
        cur = self.conn.execute(
            "INSERT INTO household_shopping "
            "(household_id, child_id, item, spec, where_to_buy, added_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self._household_id(), child_id, item.strip(), spec, where_to_buy, added_by),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_shopping_got(self, item_id: int, got: bool, *, user_id: int | None) -> bool:
        """Check an item off (or un-check it), stamping who bought it. ``True`` if changed."""
        if got:
            cur = self.conn.execute(
                "UPDATE household_shopping SET got = 1, got_by = ?, got_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND household_id = ?",
                (user_id, item_id, self._household_id()),
            )
        else:
            cur = self.conn.execute(
                "UPDATE household_shopping SET got = 0, got_by = NULL, got_at = NULL "
                "WHERE id = ? AND household_id = ?",
                (item_id, self._household_id()),
            )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_shopping_item(self, item_id: int) -> bool:
        """Delete a shopping item (scoped to the household). ``True`` if removed."""
        cur = self.conn.execute(
            "DELETE FROM household_shopping WHERE id = ? AND household_id = ?",
            (item_id, self._household_id()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def clear_got_shopping_items(self) -> int:
        """Delete every checked-off item at once; return how many were cleared.

        The bulk companion to :meth:`remove_shopping_item` — one call to sweep the
        "got it" rows off a shared list after a shop, rather than deleting each by
        hand. Still-needed items (``got = 0``) are left untouched. Scoped to the
        household.
        """
        cur = self.conn.execute(
            "DELETE FROM household_shopping WHERE got = 1 AND household_id = ?",
            (self._household_id(),),
        )
        self.conn.commit()
        return cur.rowcount

    def shopping_items(self) -> list[dict[str, Any]]:
        """All shopping items — still-needed first, each with child + who-added names."""
        rows = self.conn.execute(
            """
            SELECT s.id, s.child_id, s.item, s.spec, s.where_to_buy, s.got, s.created_at,
                   c.name AS child_name,
                   COALESCE(u.display_name, u.handle) AS added_by_name
            FROM household_shopping s
            LEFT JOIN children c
                   ON c.id = s.child_id AND c.household_id = s.household_id
            LEFT JOIN users u ON u.id = s.added_by
            WHERE s.household_id = ?
            ORDER BY s.got ASC, s.created_at ASC, s.id ASC
            """,
            (self._household_id(),),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- routines (grouping + accountability) ---------------------------------
    #
    # A routine groups chores under ONE accountable owner (RACI "A" — the parent
    # who holds the mental load) and carries the schedule its chores inherit.
    # Upsert on title like agreements/chores; removing one unlinks (never deletes)
    # its chores. `accountability_counts` powers the "carrying" facet of the
    # balance view — how many active routines each parent is answerable for.

    def set_routine(
        self,
        *,
        title: str,
        days: str = "",
        due_time: str = "",
        accountable_id: int | None = None,
        impact: str | None = None,
        enabled: bool = True,
        updated_by: int | None,
    ) -> int:
        """Upsert a routine (keyed on title within the household), returning its id.

        ``accountable_id`` is the mental-load holder (``None`` = unassigned).
        ``due_time`` may be blank (a routine that just groups chores, no clock).
        Editing never touches the chores linked under it — only the definition.
        """
        hid = self._household_id()
        self.conn.execute(
            """
            INSERT INTO household_routines
                (household_id, title, accountable_id, days, due_time, impact,
                 enabled, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (household_id, title) DO UPDATE SET
                accountable_id = excluded.accountable_id,
                days           = excluded.days,
                due_time       = excluded.due_time,
                impact         = excluded.impact,
                enabled        = excluded.enabled,
                updated_by     = excluded.updated_by,
                updated_at     = CURRENT_TIMESTAMP
            """,
            (hid, title.strip(), accountable_id, days, due_time, impact,
             1 if enabled else 0, updated_by),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM household_routines WHERE household_id = ? AND title = ?",
            (hid, title.strip()),
        ).fetchone()
        return int(row["id"])

    def set_routine_enabled(self, routine_id: int, enabled: bool) -> bool:
        """Pause or resume a routine without deleting it. ``True`` if a row changed."""
        cur = self.conn.execute(
            "UPDATE household_routines SET enabled = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND household_id = ?",
            (1 if enabled else 0, routine_id, self._household_id()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_routine(self, routine_id: int) -> bool:
        """Delete a routine; its chores survive, unlinked (``routine_id`` → NULL)."""
        hid = self._household_id()
        self.conn.execute(
            "UPDATE household_chores SET routine_id = NULL "
            "WHERE routine_id = ? AND household_id = ?",
            (routine_id, hid),
        )
        cur = self.conn.execute(
            "DELETE FROM household_routines WHERE id = ? AND household_id = ?",
            (routine_id, hid),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def routines(self) -> list[dict[str, Any]]:
        """All the household's routines, each with the accountable member's name and chore count."""
        rows = self.conn.execute(
            """
            SELECT r.id, r.title, r.accountable_id, r.days, r.due_time, r.impact,
                   r.enabled,
                   COALESCE(u.display_name, u.handle) AS accountable_name,
                   (SELECT COUNT(*) FROM household_chores c WHERE c.routine_id = r.id)
                       AS chore_count
            FROM household_routines r
            LEFT JOIN users u ON u.id = r.accountable_id
            WHERE r.household_id = ?
            ORDER BY r.due_time ASC, r.title ASC
            """,
            (self._household_id(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def routine(self, routine_id: int) -> dict[str, Any] | None:
        """One routine row (scoped to the household), or ``None`` if not found."""
        row = self.conn.execute(
            "SELECT id, title, accountable_id, days, due_time, impact, enabled "
            "FROM household_routines WHERE id = ? AND household_id = ?",
            (routine_id, self._household_id()),
        ).fetchone()
        return _row_to_dict(row)

    def accountability_counts(self) -> list[dict[str, Any]]:
        """Per-member count of *enabled* routines they're accountable for (the "carrying" facet).

        Standing state, not windowed — accountability is who currently holds the
        mental load, not an activity tally. Every active member is included (even
        one holding nothing), so the balance view shows both parents.
        """
        hid = self._household_id()
        counts: dict[int, int] = {}
        for r in self.conn.execute(
            "SELECT accountable_id AS uid, COUNT(*) AS n FROM household_routines "
            "WHERE household_id = ? AND enabled = 1 AND accountable_id IS NOT NULL "
            "GROUP BY accountable_id",
            (hid,),
        ).fetchall():
            counts[r["uid"]] = int(r["n"])
        members = self.conn.execute(
            "SELECT id, COALESCE(display_name, handle) AS name FROM users "
            "WHERE household_id = ? AND status = 'active'",
            (hid,),
        ).fetchall()
        out = [
            {"user_id": m["id"], "name": m["name"], "count": counts.get(m["id"], 0)}
            for m in members
        ]
        out.sort(key=lambda c: (-c["count"], c["name"]))
        return out

    # -- recurring shared chores ----------------------------------------------
    #
    # Household-scoped, owner-assigned recurring tasks whose whole reason to exist
    # is shared load: forgetting one lands on the other parent. Storage is plain
    # (upsert on title, a per-day completion log, two date cursors that dedup the
    # reminder and the miss-handoff); the "is it due?" timing and the message copy
    # are pure and live in prefrontal.household. `days` is stored as a weekday-int
    # CSV ("0,1,2"; empty = every day) — the pure layer parses/formats it.

    def set_chore(
        self,
        *,
        title: str,
        due_time: str,
        days: str = "",
        owner_id: int | None = None,
        routine_id: int | None = None,
        remind_before: int = 30,
        impact: str | None = None,
        enabled: bool = True,
        updated_by: int | None,
    ) -> int:
        """Upsert a recurring chore (keyed on title within the household), returning its id.

        Same last-write-wins upsert shape as :meth:`set_agreement`. ``owner_id`` is
        the member whose job it is (RACI "R"; ``None`` = either parent);
        ``routine_id`` links it under a routine (``None`` = stands alone). Editing a
        chore never clears its completion log or dedup cursors — only its definition.
        """
        hid = self._household_id()
        self.conn.execute(
            """
            INSERT INTO household_chores
                (household_id, title, owner_id, routine_id, days, due_time,
                 remind_before, impact, enabled, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (household_id, title) DO UPDATE SET
                owner_id      = excluded.owner_id,
                routine_id    = excluded.routine_id,
                days          = excluded.days,
                due_time      = excluded.due_time,
                remind_before = excluded.remind_before,
                impact        = excluded.impact,
                enabled       = excluded.enabled,
                updated_by    = excluded.updated_by,
                updated_at    = CURRENT_TIMESTAMP
            """,
            (hid, title.strip(), owner_id, routine_id, days, due_time,
             int(remind_before), impact, 1 if enabled else 0, updated_by),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM household_chores WHERE household_id = ? AND title = ?",
            (hid, title.strip()),
        ).fetchone()
        return int(row["id"])

    def set_chore_enabled(self, chore_id: int, enabled: bool) -> bool:
        """Pause or resume a chore without deleting it. ``True`` if a row changed."""
        cur = self.conn.execute(
            "UPDATE household_chores SET enabled = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND household_id = ?",
            (1 if enabled else 0, chore_id, self._household_id()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_chore(self, chore_id: int) -> bool:
        """Delete a chore and its completion log (scoped to the household)."""
        hid = self._household_id()
        self.conn.execute(
            "DELETE FROM household_chore_log WHERE chore_id = ? AND household_id = ?",
            (chore_id, hid),
        )
        cur = self.conn.execute(
            "DELETE FROM household_chores WHERE id = ? AND household_id = ?",
            (chore_id, hid),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def chores(self) -> list[dict[str, Any]]:
        """All the household's chores, each with the owner's display name.

        Ordered by due time then title for a stable list. ``owner_name`` is
        ``None`` for an unassigned ("either parent") chore.
        """
        rows = self.conn.execute(
            """
            SELECT c.id, c.title, c.owner_id, c.routine_id, c.days, c.due_time,
                   c.remind_before, c.impact, c.enabled, c.last_reminded_on,
                   c.last_missed_on,
                   COALESCE(u.display_name, u.handle) AS owner_name,
                   r.title AS routine_title
            FROM household_chores c
            LEFT JOIN users u ON u.id = c.owner_id
            LEFT JOIN household_routines r ON r.id = c.routine_id
            WHERE c.household_id = ?
            ORDER BY c.due_time ASC, c.title ASC
            """,
            (self._household_id(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def chore(self, chore_id: int) -> dict[str, Any] | None:
        """One chore row (scoped to the household), or ``None`` if not found.

        Used by the one-tap "Done" path before logging a completion, so a tap
        can't touch another household's chore (reads back ``None`` → 404).
        """
        row = self.conn.execute(
            "SELECT id, title, owner_id, days, due_time, remind_before, impact, "
            "enabled, last_reminded_on, last_missed_on "
            "FROM household_chores WHERE id = ? AND household_id = ?",
            (chore_id, self._household_id()),
        ).fetchone()
        return _row_to_dict(row)

    def chore_ids_done_on(self, done_on: str) -> set[int]:
        """The ids of chores already completed on local date ``done_on`` (a "YYYY-MM-DD")."""
        rows = self.conn.execute(
            "SELECT chore_id FROM household_chore_log "
            "WHERE household_id = ? AND done_on = ?",
            (self._household_id(), done_on),
        ).fetchall()
        return {int(r["chore_id"]) for r in rows}

    def log_chore_done(
        self, *, chore_id: int, done_on: str, done_by: int | None
    ) -> dict[str, Any] | None:
        """Mark a chore done for local date ``done_on`` (idempotent). Returns a result dict.

        Returns ``None`` if the chore isn't in this household (so a tap can 404).
        ``created`` is ``False`` when it was already logged done for that day — a
        second tap re-stamps who/when in place rather than erroring or double-logging.
        """
        hid = self._household_id()
        chore = self.conn.execute(
            "SELECT title FROM household_chores WHERE id = ? AND household_id = ?",
            (chore_id, hid),
        ).fetchone()
        if chore is None:
            return None
        already = self.conn.execute(
            "SELECT 1 FROM household_chore_log "
            "WHERE household_id = ? AND chore_id = ? AND done_on = ?",
            (hid, chore_id, done_on),
        ).fetchone() is not None
        self.conn.execute(
            """
            INSERT INTO household_chore_log (household_id, chore_id, done_on, done_by, done_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (household_id, chore_id, done_on) DO UPDATE SET
                done_by = excluded.done_by,
                done_at = CURRENT_TIMESTAMP
            """,
            (hid, chore_id, done_on, done_by),
        )
        self.conn.commit()
        return {"title": chore["title"], "created": not already, "done_on": done_on}

    def mark_chore_reminded(self, chore_id: int, on: str) -> None:
        """Stamp ``last_reminded_on`` = local date ``on`` (dedups the reminder to once/day)."""
        self.conn.execute(
            "UPDATE household_chores SET last_reminded_on = ? "
            "WHERE id = ? AND household_id = ?",
            (on, chore_id, self._household_id()),
        )
        self.conn.commit()

    def mark_chore_missed(self, chore_id: int, on: str) -> None:
        """Stamp ``last_missed_on`` = local date ``on`` (dedups the miss-handoff to once/day)."""
        self.conn.execute(
            "UPDATE household_chores SET last_missed_on = ? "
            "WHERE id = ? AND household_id = ?",
            (on, chore_id, self._household_id()),
        )
        self.conn.commit()

    # -- self-serve household setup + invites ---------------------------------
    #
    # Membership without an operator: a user with no household can create one
    # (create_own_household) and invite a co-parent with a short code
    # (create_invite), who joins by redeeming it (redeem_invite). These use the
    # bound user (_uid) but deliberately NOT _household_id — the redeemer isn't a
    # member yet, and a creator has no household until the create runs.

    def create_own_household(self, name: str) -> int:
        """Create a household and put the (bound) caller in it. Raises if already in one."""
        uid = self._uid()
        if self.household_id_or_none() is not None:
            raise ValueError("you're already in a household")
        cur = self.conn.execute("INSERT INTO households (name) VALUES (?)", (name.strip(),))
        hid = int(cur.lastrowid)
        self.conn.execute("UPDATE users SET household_id = ? WHERE id = ?", (hid, uid))
        self.conn.commit()
        return hid

    def _new_invite_code(self) -> str:
        """A short, unambiguous, unique invite code like ``"PLUM-7F2Q"`` (retrying on collision)."""
        for _ in range(12):
            code = "-".join(
                "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(4)) for _ in range(2)
            )
            if self.conn.execute(
                "SELECT 1 FROM household_invites WHERE code = ?", (code,)
            ).fetchone() is None:
                return code
        raise RuntimeError("couldn't generate a unique invite code")

    def create_invite(self, *, ttl_days: int = 7) -> dict[str, Any]:
        """Mint a shareable invite code for the caller's household (expires in ``ttl_days``)."""
        hid = self._household_id()
        code = self._new_invite_code()
        self.conn.execute(
            "INSERT INTO household_invites (household_id, code, created_by, expires_at) "
            "VALUES (?, ?, ?, datetime('now', ?))",
            # ":+d" keeps the modifier valid for negative ttls too ("-1 days"),
            # not the malformed "+-1 days".
            (hid, code, self._uid(), f"{int(ttl_days):+d} days"),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT code, expires_at FROM household_invites WHERE code = ?", (code,)
        ).fetchone()
        return {"code": row["code"], "expires_at": row["expires_at"]}

    def pending_invites(self) -> list[dict[str, Any]]:
        """Unredeemed, unexpired invites for the caller's household (newest first)."""
        rows = self.conn.execute(
            """
            SELECT i.id, i.code, i.expires_at,
                   COALESCE(u.display_name, u.handle) AS created_by_name
            FROM household_invites i
            LEFT JOIN users u ON u.id = i.created_by
            WHERE i.household_id = ? AND i.redeemed_by IS NULL
                  AND i.expires_at > datetime('now')
            ORDER BY i.created_at DESC
            """,
            (self._household_id(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def revoke_invite(self, invite_id: int) -> bool:
        """Delete an unredeemed invite (scoped to the household). ``True`` if removed."""
        cur = self.conn.execute(
            "DELETE FROM household_invites "
            "WHERE id = ? AND household_id = ? AND redeemed_by IS NULL",
            (invite_id, self._household_id()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def redeem_invite(self, code: str) -> dict[str, Any]:
        """Join the caller to the invite's household. Returns ``{ok, error?, household_name?}``.

        A global lookup by code (the caller isn't a member yet), with a specific,
        friendly reason for each failure — invalid / used / expired / already in a
        household — so the UI can just show ``error``.
        """
        uid = self._uid()
        row = self.conn.execute(
            "SELECT id, household_id, redeemed_by, "
            "(expires_at <= datetime('now')) AS expired "
            "FROM household_invites WHERE code = ?",
            (code.strip().upper(),),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "That invite code isn't valid."}
        if row["redeemed_by"] is not None:
            return {"ok": False, "error": "That invite has already been used."}
        if row["expired"]:
            return {"ok": False, "error": "That invite has expired — ask for a fresh one."}
        if self.household_id_or_none() is not None:
            return {"ok": False, "error": "You're already in a household."}
        self.conn.execute(
            "UPDATE users SET household_id = ? WHERE id = ?", (row["household_id"], uid)
        )
        self.conn.execute(
            "UPDATE household_invites SET redeemed_by = ?, redeemed_at = datetime('now') "
            "WHERE id = ?",
            (uid, row["id"]),
        )
        self.conn.commit()
        name = self.conn.execute(
            "SELECT name FROM households WHERE id = ?", (row["household_id"],)
        ).fetchone()
        return {"ok": True, "household_name": name["name"] if name else None}

    # -- operator (unscoped store) --------------------------------------------
    #
    # Membership is operator-set in v1 (docs/household-sheet.md §8): one of the
    # parents (or the deployer) wires the two users into one household once. These
    # run on the *unscoped* store, mirroring create_user / provision_user.

    def create_household(self, name: str) -> int:
        """Create a household, returning its id (operator-only, unscoped store)."""
        cur = self.conn.execute(
            "INSERT INTO households (name) VALUES (?)", (name.strip(),)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_user_household(self, handle: str, household_id: int | None) -> bool:
        """Put a user into (or, with ``None``, out of) a household. ``True`` if changed.

        Operator-only, unscoped store. Validates the target household exists
        (unless clearing) so a typo can't strand a user pointing at a phantom id.
        """
        if household_id is not None:
            exists = self.conn.execute(
                "SELECT 1 FROM households WHERE id = ?", (household_id,)
            ).fetchone()
            if exists is None:
                raise ValueError(f"no household with id {household_id}")
        cur = self.conn.execute(
            "UPDATE users SET household_id = ? WHERE handle = ?",
            (household_id, handle),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def household_members(self, household_id: int) -> list[dict[str, Any]]:
        """Users belonging to ``household_id`` (operator view), oldest first."""
        rows = self.conn.execute(
            "SELECT id, handle, display_name, status FROM users "
            "WHERE household_id = ? ORDER BY id ASC",
            (household_id,),
        ).fetchall()
        return [dict(r) for r in rows]
