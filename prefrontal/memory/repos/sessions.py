"""Escalating sessions — outings and focus sessions — over a shared core.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
)
from prefrontal.memory.repos._base import Repo


class SessionsRepo(Repo):
    """Escalating sessions — outings and focus sessions — over a shared core."""

    def _session_start(
        self,
        *,
        table: str,
        columns: list[str],
        values: list[Any],
        ts_col: str,
        ts_value: str | None,
    ) -> int:
        """Insert a session row (scoped to the user) and return its id.

        ``columns``/``values`` are the domain fields; ``user_id`` is prepended
        automatically. When ``ts_value`` is given it overrides the DB clock for
        the start timestamp (``ts_col``) — mainly for tests.
        """
        cols = ["user_id", *columns]
        vals: list[Any] = [self._uid(), *values]
        if ts_value is not None:
            cols.append(ts_col)
            vals.append(ts_value)
        placeholders = ", ".join("?" for _ in cols)
        cur = self.conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _session_get(self, table: str, session_id: int) -> dict[str, Any] | None:
        """Return a single session row by id, or ``None`` if it does not exist."""
        row = self.conn.execute(
            f"SELECT * FROM {table} WHERE id = ? AND user_id = ?",
            (session_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def _session_active(self, table: str, started_col: str) -> list[dict[str, Any]]:
        """Return active sessions with a computed ``elapsed_minutes`` field.

        Elapsed time is computed in SQL against ``CURRENT_TIMESTAMP`` (both
        timestamps are UTC), so callers never deal with timezones.
        """
        rows = self.conn.execute(
            f"SELECT *, (julianday('now') - julianday({started_col})) * 1440.0 "
            f"AS elapsed_minutes FROM {table} "
            "WHERE user_id = ? AND status = 'active' ORDER BY id",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def _session_most_recent_active(
        self, table: str, started_col: str
    ) -> dict[str, Any] | None:
        """Return the newest active session (with ``elapsed_minutes``), or ``None``."""
        row = self.conn.execute(
            f"SELECT *, (julianday('now') - julianday({started_col})) * 1440.0 "
            f"AS elapsed_minutes FROM {table} "
            "WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (self._uid(),),
        ).fetchone()
        return _row_to_dict(row)

    def _session_set_level(self, table: str, session_id: int, level: str) -> None:
        """Record the highest escalation level that has fired for a session."""
        self.conn.execute(
            f"UPDATE {table} SET last_level = ? WHERE id = ? AND user_id = ?",
            (level, session_id, self._uid()),
        )
        self.conn.commit()

    def _session_close(
        self,
        *,
        table: str,
        started_col: str,
        closed_col: str,
        session_id: int,
        status: str,
        extra_set: str = "",
        extra_params: tuple[Any, ...] = (),
        closed_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Close an active session and return it with a computed ``actual_minutes``.

        ``extra_set``/``extra_params`` let a caller write additional columns in the
        same UPDATE (e.g. focus sessions' ``COALESCE``-guarded breadcrumb/outcome).
        ``closed_at`` (a stored UTC timestamp) backdates the close instead of using
        ``CURRENT_TIMESTAMP`` — e.g. an outing return timed to when location first
        put the user home, not when they acknowledged it. Returns ``None`` if the
        session was not active.
        """
        ts_sql = "?" if closed_at is not None else "CURRENT_TIMESTAMP"
        ts_params: tuple[Any, ...] = (closed_at,) if closed_at is not None else ()
        cur = self.conn.execute(
            f"UPDATE {table} SET status = ?, {closed_col} = {ts_sql}{extra_set} "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (status, *ts_params, *extra_params, session_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        row = self.conn.execute(
            f"SELECT *, (julianday({closed_col}) - julianday({started_col})) * 1440.0 "
            f"AS actual_minutes FROM {table} WHERE id = ? AND user_id = ?",
            (session_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def _session_recent(self, table: str, limit: int) -> list[dict[str, Any]]:
        """Return recent sessions (any status), newest first."""
        rows = self.conn.execute(
            f"SELECT * FROM {table} WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def start_outing(
        self,
        intention: str,
        time_window_minutes: float,
        *,
        home_lat: float | None = None,
        home_lon: float | None = None,
        departure_at: str | None = None,
        domain: str | None = None,
    ) -> int:
        """Record a declared outing and return its id.

        Args:
            intention: The stated mission ("getting coffee").
            time_window_minutes: The stated "back in N minutes" window.
            home_lat: Optional baseline latitude.
            home_lon: Optional baseline longitude.
            departure_at: Optional ISO timestamp for the departure; defaults to
                the DB's ``CURRENT_TIMESTAMP``. Mainly useful for tests.
            domain: Optional life-sphere (shop/work/home/kids/personal) so a
                declared outing feeds the focus-balance rollup alongside passive
                trips (:mod:`prefrontal.focus_balance`); editable later via
                :meth:`set_outing_domain`.

        Returns:
            The new outing's ``id``.
        """
        return self._session_start(
            table="outings",
            columns=["intention", "time_window_minutes", "home_lat", "home_lon", "domain"],
            values=[intention, time_window_minutes, home_lat, home_lon, domain],
            ts_col="departure_at",
            ts_value=departure_at,
        )

    def set_outing_domain(
        self, outing_id: int, domain: str | None
    ) -> dict[str, Any] | None:
        """Set (or clear) an outing's life-sphere domain; return the updated outing.

        The focus-balance edit path for declared outings, mirroring
        :meth:`~prefrontal.memory.repos.trips.TripsRepo.set_trip_domain`. ``None``
        clears it. Returns ``None`` if no such outing exists for the user.
        """
        cur = self.conn.execute(
            "UPDATE outings SET domain = ? WHERE id = ? AND user_id = ?",
            (domain, outing_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_outing(outing_id)

    def set_outing_intention(
        self, outing_id: int, intention: str
    ) -> dict[str, Any] | None:
        """Correct an outing's stated mission; return the updated outing.

        The natural-language edit path (the dashboard assistant) for fixing what
        an outing was for — "change my outing to grocery run". Works on any of the
        user's outings, active or already closed (a retroactive correction).
        Returns ``None`` if no such outing exists for the user.
        """
        cur = self.conn.execute(
            "UPDATE outings SET intention = ? WHERE id = ? AND user_id = ?",
            (intention, outing_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_outing(outing_id)

    def set_outing_window(
        self, outing_id: int, time_window_minutes: float
    ) -> dict[str, Any] | None:
        """Adjust an outing's stated "back in N minutes" window; return the outing.

        The edit behind "give me 15 more minutes": on an active outing this
        re-bases the escalation level (elapsed is compared against the new window);
        on a past one it corrects the record. Returns ``None`` if no such outing
        exists for the user.
        """
        cur = self.conn.execute(
            "UPDATE outings SET time_window_minutes = ? WHERE id = ? AND user_id = ?",
            (time_window_minutes, outing_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_outing(outing_id)

    def set_outing_departure(
        self, outing_id: int, departure_at: str
    ) -> dict[str, Any] | None:
        """Correct an outing's start (``departure_at``); return the updated outing.

        The natural-language edit path for "I actually left at 2, not 2:15".
        ``departure_at`` is a stored UTC timestamp; because elapsed time (and thus
        the escalation level) is measured from it, moving it re-bases an active
        outing's clock. Works on a closed outing too, as a retroactive fix.
        Returns ``None`` if no such outing exists for the user.
        """
        cur = self.conn.execute(
            "UPDATE outings SET departure_at = ? WHERE id = ? AND user_id = ?",
            (departure_at, outing_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return self.get_outing(outing_id)

    def set_outing_home_arrived(
        self, outing_id: int, arrived_at: str | None
    ) -> None:
        """Stamp (or clear) when location first confirmed the user home on an outing.

        Set on the first ``/outing/check`` poll that finds the user within the home
        radius; cleared (``None``) if a later poll finds them away again (they left
        before ending it). Only touches an *active* outing. The stored timestamp
        both backdates the eventual return (see :meth:`close_outing`) and anchors
        the grace period before an unanswered arrival prompt auto-closes it.
        """
        self.conn.execute(
            "UPDATE outings SET home_arrived_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (arrived_at, outing_id, self._uid()),
        )
        self.conn.commit()

    def completed_outings_since(
        self, since: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Returned outings whose close landed at/after ``since`` (ISO ts), newest first.

        The declared-outing half of the focus-balance window query (the passive
        half is :meth:`~prefrontal.memory.repos.trips.TripsRepo.completed_trips_since`).
        Only ``returned`` outings count — an ``abandoned`` one auto-closed at a
        ratio of its window, so its duration isn't a trustworthy "time out". Carries
        a computed ``actual_minutes`` (departure → return) for the rollup.
        """
        rows = self.conn.execute(
            "SELECT *, (julianday(returned_at) - julianday(departure_at)) * 1440.0 "
            "AS actual_minutes FROM outings "
            "WHERE user_id = ? AND status = 'returned' AND returned_at IS NOT NULL "
            "AND returned_at >= ? ORDER BY returned_at DESC LIMIT ?",
            (self._uid(), since, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_outing(self, outing_id: int) -> dict[str, Any] | None:
        """Return a single outing by id, or ``None`` if it does not exist."""
        return self._session_get("outings", outing_id)

    def active_outings(self) -> list[dict[str, Any]]:
        """Return active outings with a computed ``elapsed_minutes`` field.

        Elapsed time is computed in SQL against ``CURRENT_TIMESTAMP`` (both
        timestamps are UTC), so callers never have to deal with timezones.

        Returns:
            A list of outing dicts, each including ``elapsed_minutes``.
        """
        return self._session_active("outings", "departure_at")

    def most_recent_active_outing(self) -> dict[str, Any] | None:
        """Return the newest active outing (with ``elapsed_minutes``), or ``None``."""
        return self._session_most_recent_active("outings", "departure_at")

    def set_outing_level(self, outing_id: int, level: str) -> None:
        """Record the highest escalation level that has fired for an outing.

        Args:
            outing_id: The outing to update.
            level: One of ``none``/``soft``/``firm``/``call``.
        """
        self._session_set_level("outings", outing_id, level)

    def close_outing(
        self, outing_id: int, status: str = "returned", *, returned_at: str | None = None
    ) -> dict[str, Any] | None:
        """Close an active outing and return it with a computed ``actual_minutes``.

        Args:
            outing_id: The outing to close.
            status: Terminal status to set (``returned`` or ``abandoned``).
            returned_at: Explicit close time (stored UTC). When omitted for a
                ``returned`` close, it backdates to the outing's ``home_arrived_at``
                if set — so the recorded time out reflects when location first put
                the user home, not when they tapped "I'm back". A location-less
                return (no ``home_arrived_at``) still closes at "now".

        Returns:
            The closed outing dict including ``actual_minutes`` (minutes between
            departure and return), or ``None`` if the outing was not active.
        """
        if returned_at is None and status == "returned":
            row = self.get_outing(outing_id)
            if row and row.get("status") == "active" and row.get("home_arrived_at"):
                returned_at = row["home_arrived_at"]
        closed = self._session_close(
            table="outings",
            started_col="departure_at",
            closed_col="returned_at",
            session_id=outing_id,
            status=status,
            closed_at=returned_at,
        )
        # The outing is over, so its "still on track?" nudge is moot — expire it
        # now (every surface reads only unexpired nudges) rather than letting it
        # linger on the default TTL. Only when a close actually happened, not a
        # re-close of an already-closed outing.
        if closed is not None:
            self.expire_nudges("outing")
        return closed

    def recent_outings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent outings (any status), newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of outing dicts ordered by ``id`` descending.
        """
        return self._session_recent("outings", limit)

    def start_focus_session(
        self,
        intended_task: str,
        *,
        planned_minutes: float | None = None,
        aligned: bool = True,
        started_at: str | None = None,
        todo_id: int | None = None,
    ) -> int:
        """Record a declared focus session and return its id.

        Args:
            intended_task: What the user is getting into ("the API refactor").
            planned_minutes: Optional intended duration. When set, it (rather
                than the soft block default) is the point past which a gentle
                alignment check fires.
            aligned: The protect bit — whether this is the thing the user meant
                to be doing. ``True`` (the default) makes the block eligible for
                the protect window.
            started_at: Optional ISO timestamp for the start; defaults to the
                DB's ``CURRENT_TIMESTAMP``. Mainly useful for tests.
            todo_id: Optional link to the todo being worked. Its energy/category
                tag the close episode so the bias can condition on them (§5).

        Returns:
            The new session's ``id``.
        """
        return self._session_start(
            table="focus_sessions",
            columns=["intended_task", "planned_minutes", "aligned", "todo_id"],
            values=[intended_task, planned_minutes, 1 if aligned else 0, todo_id],
            ts_col="started_at",
            ts_value=started_at,
        )

    def get_focus_session(self, session_id: int) -> dict[str, Any] | None:
        """Return a single focus session by id, or ``None`` if it does not exist."""
        return self._session_get("focus_sessions", session_id)

    def active_focus_sessions(self) -> list[dict[str, Any]]:
        """Return active focus sessions with a computed ``elapsed_minutes`` field.

        Elapsed time is computed in SQL against ``CURRENT_TIMESTAMP`` (both
        timestamps are UTC), so callers never have to deal with timezones.

        Returns:
            A list of session dicts, each including ``elapsed_minutes``.
        """
        return self._session_active("focus_sessions", "started_at")

    def most_recent_active_focus_session(self) -> dict[str, Any] | None:
        """Return the newest active focus session (with ``elapsed_minutes``), or ``None``."""
        return self._session_most_recent_active("focus_sessions", "started_at")

    def set_focus_session_level(self, session_id: int, level: str) -> None:
        """Record the highest interrupt level that has fired for a session.

        Args:
            session_id: The session to update.
            level: One of ``none``/``check``/``break``.
        """
        self._session_set_level("focus_sessions", session_id, level)

    def record_switch_impulse(self, session_id: int) -> bool:
        """Count one switch-impulse against an active focus session.

        Increments ``switch_impulses`` — the moment the pull to switch was
        signalled, before it's resolved. The deferral (if any) is counted
        separately by :meth:`mark_switch_deferred`, so the two-phase
        switch→resolve flow never double-counts the impulse. No-ops on a
        closed/absent session.

        Returns:
            ``True`` if a row was updated.
        """
        cur = self.conn.execute(
            "UPDATE focus_sessions SET switch_impulses = switch_impulses + 1 "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (session_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def mark_switch_deferred(self, session_id: int) -> bool:
        """Count an already-signalled switch-impulse as captured-and-deferred.

        Increments ``switches_deferred`` only (the impulse itself was counted by
        :meth:`record_switch_impulse`). Together the two counters give the
        per-session honor/defer ratio the ``context_switch`` learning pass reads.
        No-ops on a closed/absent session.

        Returns:
            ``True`` if a row was updated.
        """
        cur = self.conn.execute(
            "UPDATE focus_sessions SET switches_deferred = switches_deferred + 1 "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (session_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def close_focus_session(
        self,
        session_id: int,
        status: str = "ended",
        *,
        breadcrumb: str | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any] | None:
        """Close an active focus session and return it with ``actual_minutes``.

        ``breadcrumb`` and ``outcome`` are only written when provided (a
        ``COALESCE`` keeps any value already set), so a passive auto-close never
        clobbers a breadcrumb the user captured.

        Args:
            session_id: The session to close.
            status: Terminal status to set (``ended`` or ``abandoned``).
            breadcrumb: Optional "where I was / next step" note for cheap re-entry.
            outcome: Optional one-tap rating (``worth_it``/``should_have_stopped``/
                ``pulled_off``).

        Returns:
            The closed session dict including ``actual_minutes`` (minutes between
            start and end), or ``None`` if the session was not active.
        """
        return self._session_close(
            table="focus_sessions",
            started_col="started_at",
            closed_col="ended_at",
            session_id=session_id,
            status=status,
            extra_set=", breadcrumb = COALESCE(?, breadcrumb), "
            "outcome = COALESCE(?, outcome)",
            extra_params=(breadcrumb, outcome),
        )

    def recent_focus_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent focus sessions (any status), newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of session dicts ordered by ``id`` descending.
        """
        return self._session_recent("focus_sessions", limit)
