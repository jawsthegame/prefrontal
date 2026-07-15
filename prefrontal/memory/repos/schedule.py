"""Commitments, curated places, the geocode cache, and dismissals.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
    _with_calendar,
    sql_placeholders,
)
from prefrontal.memory.repos._base import Repo


def _lower_bound(now: datetime | str | None) -> tuple[str, tuple[Any, ...]]:
    """SQL expression + bind params for the "starting now or later" lower bound.

    ``now=None`` (the production default) uses SQLite's real clock
    (``datetime('now')``). A caller may pass an explicit naive-UTC ``now``
    (``datetime`` or ``"YYYY-MM-DD HH:MM:SS"`` string) so the window is computed
    as-of an injected clock — keeping the whole read deterministic instead of
    filtering by the wall clock while its caller windows by an injected ``now``
    (see :func:`prefrontal.household.build_sheet`).
    """
    if now is None:
        return "datetime('now')", ()
    # A UTC bind param for the UTC-stored start_at column (matches datetime('now')).
    # tz-ok: a wire/UTC comparison value, never rendered to the user as a clock.
    stamp = now if isinstance(now, str) else now.strftime("%Y-%m-%d %H:%M:%S")
    return "?", (stamp,)


class ScheduleRepo(Repo):
    """Commitments, curated places, the geocode cache, and dismissals."""

    def upsert_commitment(
        self,
        *,
        title: str,
        start_at: str,
        external_id: str | None = None,
        end_at: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        source_url: str | None = None,
        dest_lat: float | None = None,
        dest_lon: float | None = None,
        lead_minutes: float = 10.0,
        hardness: str = "soft",
        hardness_source: str | None = None,
        source: str = "calendar",
        kind: str = "self",
        kind_source: str | None = None,
        domain: str | None = None,
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
            notes: Optional user free-text detail, consulted when a nudge is built
                for this commitment. Set only on insert — a calendar re-sync
                (the update branch) never clobbers it, so a user note survives the
                same way ``hidden``/``outcome`` do (edit it via
                :meth:`set_commitment_notes`).
            source_url: Optional deeplink to the source event/email (stored
                verbatim and surfaced in the dashboard).
            dest_lat: Optional destination latitude (enables travel-time
                estimation for departure reminders).
            dest_lon: Optional destination longitude.
            lead_minutes: Travel+prep buffer needed before ``start_at``.
            hardness: ``hard`` (firm) or ``soft`` (elastic, the default).
            hardness_source: How ``hardness`` was set (``feed``/``user``/
                ``default``). The sync passes back a stored ``user`` value so a
                calendar re-sync never clobbers the user's override (see
                :meth:`hardness_by_external_id` and :meth:`set_commitment_hardness`).
            source: ``calendar`` or ``manual``.
            domain: Optional life-sphere (``work``/``home``/``kids``/…). Set only
                on insert — like ``notes``, the update branch never clobbers it, so
                a user's domain survives a calendar re-sync (edit via
                :meth:`set_commitment_domain`).

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
                    "lead_minutes = ?, hardness = ?, hardness_source = ?, "
                    "source = ?, kind = ?, kind_source = ?, status = 'active', "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND user_id = ?",
                    (title, start_at, end_at, location, source_url, dest_lat,
                     dest_lon, lead_minutes, hardness, hardness_source, source,
                     kind, kind_source, existing["id"], self._uid()),
                )
                self.conn.commit()
                return int(existing["id"]), False

        cur = self.conn.execute(
            "INSERT INTO commitments (user_id, external_id, title, start_at, end_at, "
            "location, notes, source_url, dest_lat, dest_lon, lead_minutes, hardness, "
            "hardness_source, source, kind, kind_source, domain) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._uid(), external_id, title, start_at, end_at, location, notes,
             source_url, dest_lat, dest_lon, lead_minutes, hardness,
             hardness_source, source, kind, kind_source, domain),
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

    def upcoming_commitments(
        self,
        limit: int = 50,
        *,
        include_hidden: bool = False,
        now: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """Return active commitments starting now or later, soonest first.

        Hidden commitments (the user's "don't show me this") are excluded by
        default, so they vanish from every surface that reads this — the widget,
        the dashboard, conflict detection, departure reminders. Pass
        ``include_hidden=True`` only for the un-hide affordance
        (:meth:`hidden_commitments`).

        Args:
            limit: Maximum number of rows to return.
            include_hidden: When ``True``, hidden commitments are included too.
            now: Optional naive-UTC lower bound (``datetime`` or string). Defaults
                to the wall clock; pass an injected clock to keep a caller that
                windows by its own ``now`` (e.g. the household sheet) from
                silently filtering by the real clock instead.

        Returns:
            A list of commitment dicts ordered by ``start_at`` ascending.
        """
        hidden_clause = "" if include_hidden else "AND hidden = 0 "
        bound_sql, bound_params = _lower_bound(now)
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            f"AND start_at >= {bound_sql} {hidden_clause}ORDER BY start_at ASC LIMIT ?",
            (self._uid(), *bound_params, limit),
        ).fetchall()
        return [_with_calendar(dict(r)) for r in rows]

    def hidden_commitments(
        self, limit: int = 50, *, now: datetime | str | None = None
    ) -> list[dict[str, Any]]:
        """Return active, *hidden* upcoming commitments, soonest first.

        The complement of :meth:`upcoming_commitments` — what the un-hide UI needs
        so a hidden commitment can be brought back. Empty for a user who has
        hidden nothing. ``now`` behaves as in :meth:`upcoming_commitments`.
        """
        bound_sql, bound_params = _lower_bound(now)
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            f"AND start_at >= {bound_sql} AND hidden = 1 ORDER BY start_at ASC LIMIT ?",
            (self._uid(), *bound_params, limit),
        ).fetchall()
        return [_with_calendar(dict(r)) for r in rows]

    def set_commitment_hidden(
        self, commitment_id: int, hidden: bool
    ) -> dict[str, Any] | None:
        """Hide or un-hide a commitment; return the updated row (``None`` if absent).

        ``hidden`` is deliberately *not* touched by :meth:`upsert_commitment`, so
        the flag persists across calendar re-syncs (like a user's ``kind``
        override) — a hidden event stays hidden even as its details update.
        """
        self.conn.execute(
            "UPDATE commitments SET hidden = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ?",
            (1 if hidden else 0, commitment_id, self._uid()),
        )
        self.conn.commit()
        return self.get_commitment(commitment_id)

    def previous_commitments(
        self, limit: int = 50, *, window_hours: float = 24.0
    ) -> list[dict[str, Any]]:
        """Return recently-elapsed commitments still awaiting a made/missed answer.

        The counterpart to :meth:`upcoming_commitments`: commitments whose
        effective end (``end_at``, or ``start_at`` when there's no end) has passed
        but is within the last ``window_hours``, so the dashboard can ask "did you
        make it?" for about a day and then let the row age out on its own. Excludes
        the ones already answered (``outcome`` set) and the hidden ones — so
        answering *or* hiding drops a row immediately, while an untouched one
        lingers only for the window. Most-recent first.

        Args:
            limit: Maximum number of rows to return.
            window_hours: How far back an elapsed commitment stays surfaced.

        Returns:
            A list of commitment dicts ordered by ``start_at`` descending.
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            "AND hidden = 0 AND outcome IS NULL "
            "AND datetime(COALESCE(end_at, start_at)) < datetime('now') "
            "AND datetime(COALESCE(end_at, start_at)) >= datetime('now', ?) "
            "ORDER BY start_at DESC LIMIT ?",
            (self._uid(), f"-{window_hours} hours", limit),
        ).fetchall()
        return [_with_calendar(dict(r)) for r in rows]

    def set_commitment_outcome(
        self, commitment_id: int, outcome: str | None
    ) -> dict[str, Any] | None:
        """Record (or clear) a commitment's made/missed outcome; return the row.

        ``outcome`` is a user judgement — like :meth:`set_commitment_hidden`'s
        ``hidden``, it's deliberately *not* touched by :meth:`upsert_commitment`,
        so a calendar re-sync never clobbers it. Passing ``None`` clears the
        answer (and its timestamp), surfacing the commitment again if it's still
        within the window. Returns ``None`` if no such commitment exists.
        """
        self.conn.execute(
            "UPDATE commitments SET outcome = ?, "
            "outcome_at = CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (outcome, outcome, commitment_id, self._uid()),
        )
        self.conn.commit()
        return self.get_commitment(commitment_id)

    def set_commitment_prepared(
        self, commitment_id: int, prepared: str | None
    ) -> dict[str, Any] | None:
        """Record (or clear) a past commitment's "did you feel prepared?" answer.

        A user reflection (``yes``/``no``) on an elapsed work commitment — like
        :meth:`set_commitment_outcome`'s ``outcome`` it is deliberately *not*
        touched by :meth:`upsert_commitment`, so a calendar re-sync never clobbers
        it. Passing ``None`` clears the answer (and its timestamp). Returns
        ``None`` if no such commitment exists.
        """
        self.conn.execute(
            "UPDATE commitments SET prepared = ?, "
            "prepared_at = CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (prepared, prepared, commitment_id, self._uid()),
        )
        self.conn.commit()
        return self.get_commitment(commitment_id)

    def set_commitment_notes(
        self, commitment_id: int, notes: str | None
    ) -> dict[str, Any] | None:
        """Set (or clear) a commitment's free-text notes; return the updated row.

        The note is a user field — like :meth:`set_commitment_hidden`'s ``hidden``
        and :meth:`set_commitment_outcome`'s ``outcome``, it is deliberately *not*
        touched by :meth:`upsert_commitment`'s re-sync path, so a calendar poll
        never clobbers "bring the insurance card". Passing ``None``/empty clears
        it. The note is consulted when a nudge is built for this commitment (e.g.
        the departure reminder). Returns ``None`` if no such commitment exists.
        """
        clean = notes.strip() if isinstance(notes, str) and notes.strip() else None
        self.conn.execute(
            "UPDATE commitments SET notes = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ?",
            (clean, commitment_id, self._uid()),
        )
        self.conn.commit()
        return self.get_commitment(commitment_id)

    def set_commitment_domain(
        self, commitment_id: int, domain: str | None
    ) -> dict[str, Any] | None:
        """Set (or clear) a commitment's life-sphere ``domain``; return the updated row.

        The domain is a user field — like :meth:`set_commitment_notes`'s ``notes``
        and :meth:`set_commitment_hidden`'s ``hidden``, it is deliberately *not*
        touched by :meth:`upsert_commitment`'s re-sync path, so a calendar poll
        never clobbers it. The value is stored as given (callers snap it onto the
        canonical vocabulary via
        :func:`prefrontal.focus_balance.normalize_focus_domain` first, mirroring how
        the API/CLI normalize a todo's domain); ``None``/empty clears it. Returns
        ``None`` if no such commitment exists.
        """
        clean = domain.strip() if isinstance(domain, str) and domain.strip() else None
        self.conn.execute(
            "UPDATE commitments SET domain = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ?",
            (clean, commitment_id, self._uid()),
        )
        self.conn.commit()
        return self.get_commitment(commitment_id)

    def set_commitment_project(
        self, commitment_id: int, project_id: int | None
    ) -> dict[str, Any] | None:
        """Assign (or clear) a commitment's ``project_id``; return the updated row.

        Like ``domain``/``notes``/``hidden``, ``project_id`` is a user field: it is
        deliberately not touched by :meth:`upsert_commitment`'s re-sync path, so a
        calendar poll never clobbers it. On assignment the project's ``domain`` is
        written through (a project is nested under one life sphere, so its members
        inherit it); clearing keeps the current domain. Returns ``None`` if the
        commitment doesn't exist, or the project doesn't belong to this user.
        """
        if project_id is not None:
            project = self.get_project(project_id)  # type: ignore[attr-defined]
            if project is None:
                return None
            self.conn.execute(
                "UPDATE commitments SET project_id = ?, domain = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (project_id, project.get("domain"), commitment_id, self._uid()),
            )
        else:
            self.conn.execute(
                "UPDATE commitments SET project_id = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (commitment_id, self._uid()),
            )
        self.conn.commit()
        return self.get_commitment(commitment_id)

    def commitments_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Return active, non-hidden commitments starting in ``[start, end)``, soonest first.

        Hidden commitments are excluded (see :meth:`upcoming_commitments`) so the
        briefing, panic view, and free-time gaps never count a commitment the user
        has hidden.

        Args:
            start: Inclusive UTC lower bound (``YYYY-MM-DD HH:MM:SS``).
            end: Exclusive UTC upper bound.

        Returns:
            A list of commitment dicts (e.g. "today's" commitments for the briefing).
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            "AND hidden = 0 AND start_at >= ? AND start_at < ? ORDER BY start_at ASC",
            (self._uid(), start, end),
        ).fetchall()
        return [_with_calendar(dict(r)) for r in rows]

    def active_commitments_between(
        self, start: str, end: str, *, default_event_minutes: float = 30.0
    ) -> list[dict[str, Any]]:
        """Return active, non-hidden commitments *overlapping* ``[start, end)``, soonest first.

        The overlap-aware companion to :meth:`commitments_between`. That method
        filters by ``start_at`` alone, so a still-running multi-day / all-day block
        (a week-long conference, a two-week vacation) whose ``start_at`` predates
        ``start`` is missed — and a free-window computation over the result would
        read the user as free while they're actually busy. This method instead
        returns every commitment that *touches* the window: it starts before ``end``
        **and** is still ongoing at or after ``start``.

        A commitment's effective end is ``end_at`` when set, else
        ``start_at + default_event_minutes`` — the same assumed duration
        :func:`prefrontal.scheduling.free_windows` uses when carving gaps, so a
        point event with no ``end_at`` is bounded by its assumed length rather than
        dragging the whole past into the result. The precise clipping to the band
        still happens in ``free_windows``; this query only decides membership, so it
        errs toward *including* a borderline row (``free_windows`` drops any that
        turn out not to overlap).

        Args:
            start: Inclusive UTC lower bound of the window (``YYYY-MM-DD HH:MM:SS``).
            end: Exclusive UTC upper bound.
            default_event_minutes: Assumed length of a commitment with no ``end_at``,
                for deciding whether it's still ongoing at ``start``.

        Returns:
            A list of commitment dicts ordered by ``start_at`` ascending — a superset
            of :meth:`commitments_between`'s result over the same window, adding the
            in-progress blocks that began before it.
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            "AND hidden = 0 AND start_at < ? "
            "AND datetime(COALESCE(end_at, datetime(start_at, ?))) > datetime(?) "
            "ORDER BY start_at ASC",
            (self._uid(), end, f"+{int(default_event_minutes)} minutes", start),
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
        placeholders = sql_placeholders(len(ids))
        rows = self.conn.execute(
            f"SELECT external_id, kind, kind_source FROM commitments "
            f"WHERE user_id = ? AND external_id IN ({placeholders})",
            [self._uid(), *ids],
        ).fetchall()
        return {r["external_id"]: (r["kind"], r["kind_source"]) for r in rows}

    def hardness_by_external_id(
        self, external_ids: set[str]
    ) -> dict[str, tuple[str, str | None]]:
        """Return ``{external_id: (hardness, hardness_source)}`` for the given ids.

        The hardness twin of :meth:`kinds_by_external_id`: the sync uses it to keep
        a user's ``hardness`` override (``hardness_source == 'user'``) across a
        re-sync, rather than letting the feed reset it. Ids absent from the table
        are simply missing from the result.
        """
        if not external_ids:
            return {}
        ids = list(external_ids)
        placeholders = sql_placeholders(len(ids))
        rows = self.conn.execute(
            f"SELECT external_id, hardness, hardness_source FROM commitments "
            f"WHERE user_id = ? AND external_id IN ({placeholders})",
            [self._uid(), *ids],
        ).fetchall()
        return {r["external_id"]: (r["hardness"], r["hardness_source"]) for r in rows}

    def set_commitment_hardness(
        self, commitment_id: int, hardness: str
    ) -> dict[str, Any] | None:
        """Set a commitment's ``hardness`` as a user override; return the updated row.

        Stamps ``hardness_source = 'user'`` so the choice is sticky — the sync's
        :meth:`hardness_by_external_id` reuse keeps it across calendar re-syncs, the
        same way a user's ``kind`` correction survives. Returns ``None`` if no such
        commitment exists.
        """
        self.conn.execute(
            "UPDATE commitments SET hardness = ?, hardness_source = 'user', "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (hardness, commitment_id, self._uid()),
        )
        self.conn.commit()
        return self.get_commitment(commitment_id)

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
        self,
        name: str,
        lat: float,
        lon: float,
        *,
        label: str | None = None,
        domain: str | None = None,
    ) -> int:
        """Insert or replace a curated place alias, returning its id.

        ``name`` is the normalized match key (unique); re-adding the same name
        updates its coordinates in place. ``domain`` is an optional life-sphere
        (shop/work/home/kids/personal) — when set, a closed-loop trip whose stop
        lands here pre-fills its focus-balance domain
        (:func:`prefrontal.trips.suggest_trip_labeling`). It is ``COALESCE``-d, so a
        re-add that omits it keeps a previously-set sphere (clear it with
        :meth:`set_place_domain`).

        Args:
            name: Normalized match key (e.g. ``"gym"``).
            lat: Latitude in degrees.
            lon: Longitude in degrees.
            label: Optional original spelling for display.
            domain: Optional life-sphere this place counts toward.

        Returns:
            The place's ``id``.
        """
        return self._upsert_returning_id(
            "INSERT INTO places (user_id, name, label, lat, lon, domain) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, name) DO UPDATE SET label = excluded.label, "
            "lat = excluded.lat, lon = excluded.lon, "
            "domain = COALESCE(excluded.domain, places.domain)",
            (self._uid(), name, label, lat, lon, domain),
            select_sql="SELECT id FROM places WHERE user_id = ? AND name = ?",
            select_params=(self._uid(), name),
        )

    def set_place_domain(self, name: str, domain: str | None) -> bool:
        """Set (or clear) a curated place's life-sphere; ``True`` if a row changed.

        The edit path for a place's focus-balance domain without re-entering its
        coordinates. ``None`` clears it. Scoped to this user; ``False`` when there
        is no such place.
        """
        cur = self.conn.execute(
            "UPDATE places SET domain = ? WHERE user_id = ? AND name = ?",
            (domain, self._uid(), name),
        )
        self.conn.commit()
        return cur.rowcount > 0

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

    def delete_place(self, name: str) -> bool:
        """Delete a curated place by its normalized ``name``. ``True`` if removed.

        Scoped to this user (a place's ``name`` is unique per user), so one user
        can't delete another's. ``False`` when there was no such place — the
        route turns that into a 404. A rename is add-new + delete-old at the
        caller, since :meth:`add_place` upserts by name.
        """
        cur = self.conn.execute(
            "DELETE FROM places WHERE user_id = ? AND name = ?",
            (self._uid(), name),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_geocode_cache(self, query: str) -> dict[str, Any] | None:
        """Return a cached geocode row for ``query``, or ``None`` if not cached.

        A returned row may have ``lat``/``lon`` of ``None`` — a recorded *miss*
        (the geocoder was asked and found nothing), distinct from "never asked"
        (``None`` return).
        """
        return self._query_one("SELECT * FROM geocode_cache WHERE query = ?", (query,))

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
        """Record that the user dismissed a conflict pair (idempotent).

        The pair may be a soft possible conflict or a firm double-booking — the
        signature is opaque here (see ``commitments.conflict_dismissal_key``)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO dismissed_conflicts (user_id, signature) "
            "VALUES (?, ?)",
            (self._uid(), signature),
        )
        self.conn.commit()

    def dismissed_conflicts(self) -> set[str]:
        """Return the set of dismissed conflict signatures (possible or hard)."""
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
