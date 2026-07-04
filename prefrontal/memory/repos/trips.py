"""Closed-loop trips — leave-home → return-home round trips detected passively
from location pings crossing the home radius.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.

A *trip* differs from an *outing* (:mod:`prefrontal.memory.repos.sessions`): an
outing is a *declared* intention with a stated window that escalates in real
time, while a trip is *auto-detected* from location alone and reviewed after the
fact — the user is asked to label/categorize it and can reflect on how it went.
The two share the generic escalating-session CRUD helpers (``_session_*`` on the
:class:`SessionsRepo`) for the parts that overlap (open with a start timestamp,
close with a computed ``actual_minutes``, recent-list); the trip-specific
review columns (label/category/reflection) have their own small methods.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory.repos._base import Repo


class TripsRepo(Repo):
    """Closed-loop trips auto-detected from location crossings."""

    def open_trip(
        self,
        *,
        depart_lat: float | None = None,
        depart_lon: float | None = None,
        departed_at: str | None = None,
    ) -> int:
        """Open a trip when the phone first leaves the home radius; return its id.

        Args:
            depart_lat: The home fix the loop opened from (optional).
            depart_lon: The home fix the loop opened from (optional).
            departed_at: Optional ISO timestamp for the departure; defaults to the
                DB's ``CURRENT_TIMESTAMP``. Mainly useful for tests.

        Returns:
            The new trip's ``id``.
        """
        return self._session_start(
            table="trips",
            columns=["depart_lat", "depart_lon"],
            values=[depart_lat, depart_lon],
            ts_col="departed_at",
            ts_value=departed_at,
        )

    def get_trip(self, trip_id: int) -> dict[str, Any] | None:
        """Return a single trip by id, or ``None`` if it does not exist."""
        return self._session_get("trips", trip_id)

    def active_trip(self) -> dict[str, Any] | None:
        """Return the currently-open trip (with ``elapsed_minutes``), or ``None``.

        Only one trip is open at a time — it opens on leaving home and closes on
        returning — so this is the state-machine handle the location path uses to
        decide whether a ping is a *depart* (no active trip) or a *return*
        (an active trip while back within the home radius).
        """
        return self._session_most_recent_active("trips", "departed_at")

    def bump_trip_distance(self, trip_id: int, distance_m: float) -> None:
        """Grow an active trip's ``max_distance_m`` to at least ``distance_m``.

        Called on each away-from-home ping so a completed trip records how far the
        loop actually reached (context for labeling — a 12 km loop reads as a real
        outing, a 200 m one as a walk round the block). No-ops on a closed trip.
        """
        self.conn.execute(
            "UPDATE trips SET max_distance_m = MAX(max_distance_m, ?) "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (float(distance_m), trip_id, self._uid()),
        )
        self.conn.commit()

    def close_trip(self, trip_id: int) -> dict[str, Any] | None:
        """Close an open trip as ``completed``; return it with ``actual_minutes``.

        This is what makes the round trip a *closed loop*: it stamps
        ``returned_at`` and computes the minutes out (departure → return).

        Returns:
            The closed trip dict including ``actual_minutes``, or ``None`` if the
            trip was not active (e.g. a double return ping).
        """
        return self._session_close(
            table="trips",
            started_col="departed_at",
            closed_col="returned_at",
            session_id=trip_id,
            status="completed",
        )

    def recent_trips(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent trips (any status), newest first."""
        return self._session_recent("trips", limit)

    def unlabeled_trips(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return completed trips still awaiting a label, newest first.

        These are the ones the trip-tracking module asks the user to name and
        categorize. A trip is "unlabeled" until :meth:`label_trip` sets its
        ``label``.
        """
        rows = self.conn.execute(
            "SELECT * FROM trips WHERE user_id = ? AND status = 'completed' "
            "AND (label IS NULL OR label = '') ORDER BY id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def label_trip(
        self,
        trip_id: int,
        *,
        label: str,
        category: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any] | None:
        """Record the user's label, category, and life-domain for a completed trip.

        ``category`` (errand/social/…) is *what kind of activity* the trip was;
        ``domain`` (shop/work/home/kids/personal) is *which sphere of life* it served —
        the axis :mod:`prefrontal.focus_balance` rolls time-out up by. Both are
        ``COALESCE``-d, so a follow-up label that omits one keeps the earlier value
        (edit either with :meth:`set_trip_domain` / a re-label).

        Returns the updated trip, or ``None`` if no such trip exists for the user.
        """
        cur = self.conn.execute(
            "UPDATE trips SET label = ?, category = COALESCE(?, category), "
            "domain = COALESCE(?, domain) WHERE id = ? AND user_id = ?",
            (label, category, domain, trip_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_trip(trip_id)

    def set_trip_domain(
        self, trip_id: int, domain: str | None
    ) -> dict[str, Any] | None:
        """Set (or clear) a trip's life-sphere domain; return the updated trip.

        The focus-balance guardrail: ``domain`` (shop/work/home/kids/personal) is what
        :mod:`prefrontal.focus_balance` sums time-out by, so a misfiled trip can be
        recategorized without re-labeling it. ``None`` clears it. Returns ``None``
        if no such trip exists for the user.
        """
        cur = self.conn.execute(
            "UPDATE trips SET domain = ? WHERE id = ? AND user_id = ?",
            (domain, trip_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_trip(trip_id)

    def completed_trips_since(self, since: str, limit: int = 500) -> list[dict[str, Any]]:
        """Completed trips whose return landed at/after ``since`` (ISO ts), newest first.

        The window query behind the focus-balance rollup
        (:func:`prefrontal.focus_balance.build_focus_balance`): only closed loops
        with a ``returned_at`` count, since an open trip has no measurable time-out
        yet. ``limit`` caps a pathological backlog at personal scale.
        """
        rows = self.conn.execute(
            "SELECT * FROM trips WHERE user_id = ? AND status = 'completed' "
            "AND returned_at IS NOT NULL AND returned_at >= ? "
            "ORDER BY returned_at DESC LIMIT ?",
            (self._uid(), since, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_trip_reflection(
        self, trip_id: int, *, reflection: str, outcome: str | None = None
    ) -> dict[str, Any] | None:
        """Store a plain-English reflection (and its classified outcome) on a trip.

        Returns the updated trip, or ``None`` if no such trip exists for the user.
        """
        cur = self.conn.execute(
            "UPDATE trips SET reflection = ?, reflection_outcome = ? "
            "WHERE id = ? AND user_id = ?",
            (reflection, outcome, trip_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_trip(trip_id)

    def set_trip_episode(self, trip_id: int, episode_id: int) -> None:
        """Link the ``task`` episode a trip's return logged, for later resolution."""
        self.conn.execute(
            "UPDATE trips SET episode_id = ? WHERE id = ? AND user_id = ?",
            (episode_id, trip_id, self._uid()),
        )
        self.conn.commit()
