"""Triage decision log — audit trail + idempotency for the triage agent.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
Per-user scoped like every other repo (`self._uid()` is injected into every
statement). Backs ``GET /triage/recent`` and the idempotency check that keeps a
re-delivered signal from routing twice — see docs/triage-agent.md §7.
"""
from __future__ import annotations

from typing import Any

from prefrontal.clock import TS_FMT, utcnow
from prefrontal.memory._helpers import _row_to_dict


class TriageRepo:
    """Triage decision logging + queries."""

    def log_triage(
        self,
        *,
        source: str,
        title: str,
        kind: str,
        urgency: str,
        route: str,
        reason: str,
        confidence: float,
        decided_by: str,
        external_id: str = "",
        routed_ref: str | None = None,
        received_at: str | None = None,
    ) -> int:
        """Record one triage decision and return its new id.

        Args:
            source: The signal's source (``mail``/``calendar``/``shortcut``/…).
            title: The signal's subject/title (for the recent-decisions view).
            kind/urgency/route: The classification (see :mod:`prefrontal.triage`).
            reason: One-line, human-readable justification (always populated).
            confidence: 0.0–1.0 classifier confidence.
            decided_by: ``"heuristic"`` or ``"llm"`` — which path decided.
            external_id: Provider id for idempotency (``""`` = none/not dedup-able).
            routed_ref: The row this created, e.g. ``"todo:42"``; ``None`` on drop.
            received_at: Signal receipt timestamp; defaults to now (UTC).
        """
        cur = self.conn.execute(
            "INSERT INTO triage_log (user_id, received_at, source, external_id, title, "
            "kind, urgency, route, reason, confidence, decided_by, routed_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._uid(),
                received_at or utcnow().strftime(TS_FMT),
                source,
                external_id or "",
                title,
                kind,
                urgency,
                route,
                reason,
                float(confidence),
                decided_by,
                routed_ref,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_triage(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the caller's most recent triage decisions, newest first.

        Args:
            limit: Maximum number of rows to return.
        """
        rows = self.conn.execute(
            "SELECT * FROM triage_log WHERE user_id = ? "
            "ORDER BY received_at DESC, id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def surfaced_triage(self, since: str) -> list[dict[str, Any]]:
        """Return ``route='surface'`` decisions at/after ``since`` (newest first).

        The briefing pulls these — signals worth seeing once but with no core-table
        write — into its "worth a look" section. ``since`` bounds it to a recent
        window (the briefing uses the last day) so old surfaced items age out of
        view without being deleted.
        """
        rows = self.conn.execute(
            "SELECT * FROM triage_log WHERE user_id = ? AND route = 'surface' "
            "AND received_at >= ? ORDER BY received_at DESC, id DESC",
            (self._uid(), since),
        ).fetchall()
        return [dict(r) for r in rows]

    def triage_seen(self, source: str, external_id: str) -> dict[str, Any] | None:
        """Return the prior decision for ``(source, external_id)``, or ``None``.

        The idempotency check: before routing a signal, callers look it up here;
        a hit means re-delivery, so they return the prior decision instead of
        creating a second row. A blank ``external_id`` is never dedup-able (the
        partial unique index excludes it), so this returns ``None`` for it.
        """
        if not external_id:
            return None
        row = self.conn.execute(
            "SELECT * FROM triage_log WHERE user_id = ? AND source = ? AND external_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (self._uid(), source, external_id),
        ).fetchone()
        return _row_to_dict(row)
