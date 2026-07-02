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
from typing import Any

from prefrontal.memory._helpers import _row_to_dict

#: The controlled vocabulary for ``household_facts.category`` — a small, fixed set
#: mirroring ``todos.KNOWN_CATEGORIES``' single-source-of-truth pattern, so the
#: assistant validates against it and the render groups by it in a stable order.
FACT_CATEGORIES: tuple[str, ...] = (
    "sizes",    # tops / bottoms / shoes + brand notes
    "routine",  # wake / breakfast / ready-by / bedtime
    "food",     # allergies + severity + EpiPen, likes/dislikes
    "health",   # pediatrician / dentist / insurance / meds
    "school",   # teacher / room / activities / pickup
    "contact",  # role -> name / phone (a facts facet; see docs §3.6)
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


class HouseholdRepo:
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
                   a.updated_by, a.updated_at,
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
