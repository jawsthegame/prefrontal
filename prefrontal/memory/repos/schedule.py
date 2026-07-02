"""Commitments, curated places, the geocode cache, and dismissals.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
    _with_calendar,
)


class ScheduleRepo:
    """Commitments, curated places, the geocode cache, and dismissals."""

    def upsert_commitment(
        self,
        *,
        title: str,
        start_at: str,
        external_id: str | None = None,
        end_at: str | None = None,
        location: str | None = None,
        source_url: str | None = None,
        dest_lat: float | None = None,
        dest_lon: float | None = None,
        lead_minutes: float = 10.0,
        hardness: str = "soft",
        source: str = "calendar",
        kind: str = "self",
        kind_source: str | None = None,
    ) -> tuple[int, bool]:
        """Insert or update a commitment, returning ``(id, created)``.

        When ``external_id`` is given and already exists, the row is updated in
        place (and re-activated) — so re-syncing a calendar is idempotent.
        Timestamps should already be normalized to UTC (see
        :func:`prefrontal.commitments.to_utc`).

        Args:
            title: Commitment title.
            start_at: UTC start timestamp (``YYYY-MM-DD HH:MM:SS``).
            external_id: Calendar event id, or ``None`` for a manual entry.
            end_at: Optional UTC end timestamp.
            location: Optional free-text location.
            source_url: Optional deeplink to the source event/email (stored
                verbatim and surfaced in the dashboard).
            dest_lat: Optional destination latitude (enables travel-time
                estimation for departure reminders).
            dest_lon: Optional destination longitude.
            lead_minutes: Travel+prep buffer needed before ``start_at``.
            hardness: ``hard`` or ``soft``.
            source: ``calendar`` or ``manual``.

        Returns:
            ``(id, created)`` where ``created`` is ``True`` for a new row.
        """
        if external_id is not None:
            existing = self.conn.execute(
                "SELECT id FROM commitments WHERE user_id = ? AND external_id = ?",
                (self._uid(), external_id),
            ).fetchone()
            if existing is not None:
                self.conn.execute(
                    "UPDATE commitments SET title = ?, start_at = ?, end_at = ?, "
                    "location = ?, source_url = ?, dest_lat = ?, dest_lon = ?, "
                    "lead_minutes = ?, hardness = ?, source = ?, kind = ?, "
                    "kind_source = ?, status = 'active', "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND user_id = ?",
                    (title, start_at, end_at, location, source_url, dest_lat,
                     dest_lon, lead_minutes, hardness, source, kind, kind_source,
                     existing["id"], self._uid()),
                )
                self.conn.commit()
                return int(existing["id"]), False

        cur = self.conn.execute(
            "INSERT INTO commitments (user_id, external_id, title, start_at, end_at, "
            "location, source_url, dest_lat, dest_lon, lead_minutes, hardness, "
            "source, kind, kind_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._uid(), external_id, title, start_at, end_at, location, source_url,
             dest_lat, dest_lon, lead_minutes, hardness, source, kind, kind_source),
        )
        self.conn.commit()
        return int(cur.lastrowid), True

    def get_commitment(self, commitment_id: int) -> dict[str, Any] | None:
        """Return a single commitment by id, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM commitments WHERE id = ? AND user_id = ?",
            (commitment_id, self._uid()),
        ).fetchone()
        d = _row_to_dict(row)
        return _with_calendar(d) if d is not None else None

    def upcoming_commitments(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return active commitments starting now or later, soonest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of commitment dicts ordered by ``start_at`` ascending.
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            "AND start_at >= datetime('now') ORDER BY start_at ASC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [_with_calendar(dict(r)) for r in rows]

    def commitments_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Return active commitments starting in ``[start, end)``, soonest first.

        Args:
            start: Inclusive UTC lower bound (``YYYY-MM-DD HH:MM:SS``).
            end: Exclusive UTC upper bound.

        Returns:
            A list of commitment dicts (e.g. "today's" commitments for the briefing).
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            "AND start_at >= ? AND start_at < ? ORDER BY start_at ASC",
            (self._uid(), start, end),
        ).fetchall()
        return [_with_calendar(dict(r)) for r in rows]

    def cancel_commitment(self, commitment_id: int) -> bool:
        """Mark a commitment cancelled. Returns ``True`` if a row changed."""
        cur = self.conn.execute(
            "UPDATE commitments SET status = 'cancelled', "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (commitment_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def kinds_by_external_id(
        self, external_ids: set[str]
    ) -> dict[str, tuple[str, str | None]]:
        """Return ``{external_id: (kind, kind_source)}`` for the given ids.

        Used by the sync to reuse an already-decided ``kind`` (so a recurring
        event isn't re-classified every poll, and a user's correction is never
        clobbered by a fresh LLM verdict). Ids absent from the table are simply
        missing from the result.
        """
        if not external_ids:
            return {}
        ids = list(external_ids)
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT external_id, kind, kind_source FROM commitments "
            f"WHERE user_id = ? AND external_id IN ({placeholders})",
            [self._uid(), *ids],
        ).fetchall()
        return {r["external_id"]: (r["kind"], r["kind_source"]) for r in rows}

    def set_commitment_kind(
        self, commitment_id: int, kind: str, source: str
    ) -> dict[str, Any] | None:
        """Set a commitment's ``kind`` (and how it was set); return the updated row.

        Returns ``None`` if no such commitment exists.
        """
        self.conn.execute(
            "UPDATE commitments SET kind = ?, kind_source = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (kind, source, commitment_id, self._uid()),
        )
        self.conn.commit()
        return self.get_commitment(commitment_id)

    def record_kind_feedback(
        self, title: str, kind: str, *, llm_kind: str | None = None
    ) -> None:
        """Record a ``self``/``fyi`` label for a title (latest verdict wins).

        Keyed by the normalized (lowercased, trimmed) title so repeated
        corrections to the same event collapse to one row. These rows seed the
        classifier's few-shot examples (see :func:`prefrontal.classify`).
        """
        display = (title or "").strip()
        norm = display.lower()
        if not norm:
            return
        self.conn.execute(
            "INSERT INTO kind_feedback (user_id, title, display, kind, llm_kind) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, title) DO UPDATE SET display = excluded.display, "
            "kind = excluded.kind, llm_kind = excluded.llm_kind, "
            "updated_at = CURRENT_TIMESTAMP",
            (self._uid(), norm, display, kind, llm_kind),
        )
        self.conn.commit()

    def kind_feedback_examples(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return learned kind labels, most-recently-corrected first.

        Folded into the classifier prompt as few-shot examples so the model's
        verdicts evolve toward the user's corrections.
        """
        rows = self.conn.execute(
            "SELECT title, display, kind, llm_kind FROM kind_feedback "
            "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_commitment_coords(
        self, commitment_id: int, lat: float, lon: float
    ) -> bool:
        """Set a commitment's destination coordinates. ``True`` if a row changed.

        Used by the geocoding enrichment pass to fill ``dest_lat``/``dest_lon``
        on a commitment whose location was resolved to a point.
        """
        cur = self.conn.execute(
            "UPDATE commitments SET dest_lat = ?, dest_lon = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (lat, lon, commitment_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def commitments_needing_geocode(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return active upcoming commitments that have a location but no coords.

        These are the candidates for the geocoding enrichment pass: a free-text
        ``location`` is present but ``dest_lat``/``dest_lon`` are still unset.

        Args:
            limit: Maximum number of rows to return (bounds work per pass).

        Returns:
            A list of commitment dicts ordered by ``start_at`` ascending (soonest
            first), so the most imminent commitments get coordinates first.
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            "AND start_at >= datetime('now') AND location IS NOT NULL "
            "AND location != '' AND dest_lat IS NULL "
            "ORDER BY start_at ASC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_place(
        self, name: str, lat: float, lon: float, *, label: str | None = None
    ) -> int:
        """Insert or replace a curated place alias, returning its id.

        ``name`` is the normalized match key (unique); re-adding the same name
        updates its coordinates in place.

        Args:
            name: Normalized match key (e.g. ``"gym"``).
            lat: Latitude in degrees.
            lon: Longitude in degrees.
            label: Optional original spelling for display.

        Returns:
            The place's ``id``.
        """
        self.conn.execute(
            "INSERT INTO places (user_id, name, label, lat, lon) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, name) DO UPDATE SET label = excluded.label, "
            "lat = excluded.lat, lon = excluded.lon",
            (self._uid(), name, label, lat, lon),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM places WHERE user_id = ? AND name = ?",
            (self._uid(), name),
        ).fetchone()
        return int(row["id"])

    def places(self) -> list[dict[str, Any]]:
        """Return all curated places, longest name first.

        Longest-first ordering lets a matcher prefer the most specific alias
        (e.g. ``"dentist office"`` before ``"office"``).
        """
        rows = self.conn.execute(
            "SELECT * FROM places WHERE user_id = ? "
            "ORDER BY length(name) DESC, name ASC",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_geocode_cache(self, query: str) -> dict[str, Any] | None:
        """Return a cached geocode row for ``query``, or ``None`` if not cached.

        A returned row may have ``lat``/``lon`` of ``None`` — a recorded *miss*
        (the geocoder was asked and found nothing), distinct from "never asked"
        (``None`` return).
        """
        row = self.conn.execute(
            "SELECT * FROM geocode_cache WHERE query = ?", (query,)
        ).fetchone()
        return _row_to_dict(row)

    def set_geocode_cache(
        self, query: str, lat: float | None, lon: float | None
    ) -> None:
        """Cache a geocode result for ``query`` (``lat``/``lon`` ``None`` = miss)."""
        self.conn.execute(
            "INSERT INTO geocode_cache (query, lat, lon, last_updated) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT (query) DO UPDATE SET lat = excluded.lat, "
            "lon = excluded.lon, last_updated = CURRENT_TIMESTAMP",
            (query, lat, lon),
        )
        self.conn.commit()

    def cancel_missing_calendar(self, keep_external_ids: set[str]) -> int:
        """Cancel future calendar commitments absent from a fresh sync.

        Manual commitments are never touched. Pruning is **feed-aware**: an
        ``external_id`` may be namespaced ``feed:id`` (e.g. ``personal:…``,
        ``work:…``), and only commitments whose namespace appears in this batch
        are eligible for cancellation. That way syncing one calendar never
        cancels another calendar's events. If the batch uses no namespaces, the
        legacy behavior applies (prune any missing calendar commitment).

        Args:
            keep_external_ids: The ``external_id``\\ s present in the new sync.

        Returns:
            The number of commitments cancelled.
        """
        keep = set(keep_external_ids)
        namespaces = {e.split(":", 1)[0] for e in keep if ":" in e}
        rows = self.conn.execute(
            "SELECT id, external_id FROM commitments "
            "WHERE user_id = ? AND source = 'calendar' "
            "AND status = 'active' AND start_at >= datetime('now')",
            (self._uid(),),
        ).fetchall()
        cancelled = 0
        for row in rows:
            eid = row["external_id"]
            if eid in keep:
                continue
            if namespaces:
                ns = eid.split(":", 1)[0] if eid and ":" in eid else None
                if ns not in namespaces:
                    continue  # belongs to a feed not part of this sync; leave it
            self.conn.execute(
                "UPDATE commitments SET status = 'cancelled', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (row["id"], self._uid()),
            )
            cancelled += 1
        self.conn.commit()
        return cancelled

    def dismiss_conflict(self, signature: str) -> None:
        """Record that the user dismissed a possible-conflict pair (idempotent)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO dismissed_conflicts (user_id, signature) "
            "VALUES (?, ?)",
            (self._uid(), signature),
        )
        self.conn.commit()

    def dismissed_conflicts(self) -> set[str]:
        """Return the set of dismissed possible-conflict signatures."""
        rows = self.conn.execute(
            "SELECT signature FROM dismissed_conflicts WHERE user_id = ?",
            (self._uid(),),
        ).fetchall()
        return {r["signature"] for r in rows}

    def dismiss_departure(self, commitment_id: int) -> None:
        """Record that the user waved off departure nudges for a commitment.

        Idempotent. Keyed by ``commitment_id``, which is unique per commitment
        *occurrence* — a future occurrence is a fresh row with a new id, so the
        reminder re-arms naturally rather than being silenced forever.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO dismissed_departures (user_id, commitment_id) "
            "VALUES (?, ?)",
            (self._uid(), commitment_id),
        )
        self.conn.commit()

    def dismissed_departures(self) -> set[int]:
        """Return the set of commitment ids whose departure nudges were dismissed."""
        rows = self.conn.execute(
            "SELECT commitment_id FROM dismissed_departures WHERE user_id = ?",
            (self._uid(),),
        ).fetchall()
        return {r["commitment_id"] for r in rows}
