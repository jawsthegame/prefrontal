"""Candidate updates proposed by the LLM sensor, awaiting human confirmation.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone. A
proposal is a *pending* structured update (a coaching_state key/value or an
episode) the sensor derived from free text — it becomes real only when a human
accepts it (see :mod:`prefrontal.sensor`).
"""
from __future__ import annotations

import json
from typing import Any

from prefrontal.memory.repos._base import Repo


class ProposalsRepo(Repo):
    """Pending/accepted/rejected candidate updates from the LLM sensor."""

    def add_proposal(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        rationale: str | None = None,
        source: str = "llm_inferred",
    ) -> int:
        """Record a pending proposal and return its id.

        Args:
            kind: ``"state"`` (a coaching_state key/value) or ``"episode"``.
            payload: The proposed update, stored as JSON.
            rationale: Why the model proposed it — a quote/paraphrase of the text.
            source: Provenance stamp carried onto the write when accepted.

        Returns:
            The new proposal row's id.
        """
        cur = self.conn.execute(
            "INSERT INTO proposals (user_id, kind, payload, rationale, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (self._uid(), kind, json.dumps(payload), rationale, source),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_proposals(self, status: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
        """Return this user's proposals (newest first), payload parsed back to a dict.

        Args:
            status: Filter to this status, or ``""`` for any status.
            limit: Maximum rows to return.
        """
        rows = self.conn.execute(
            "SELECT id, kind, payload, rationale, source, status, created_at, resolved_at "
            "FROM proposals WHERE user_id = ? AND (? = '' OR status = ?) "
            "ORDER BY id DESC LIMIT ?",
            (self._uid(), status, status, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def all_resolved_proposals(self) -> list[dict[str, Any]]:
        """Return every accepted/rejected proposal (payload parsed), oldest first.

        The sensor-calibration pass
        (:func:`prefrontal.sensor.compute_sensor_calibration`) aggregates the
        sensor's full *resolved* history to measure its precision. Pending rows
        are excluded — they carry no accept/reject signal yet. Volume is
        human-paced (a person reviews these), so loading all rows is fine, the
        same call shape as ``all_episodes``.
        """
        rows = self.conn.execute(
            "SELECT id, kind, payload, rationale, source, status, created_at, resolved_at "
            "FROM proposals WHERE user_id = ? AND status IN ('accepted', 'rejected') "
            "ORDER BY id ASC",
            (self._uid(),),
        ).fetchall()
        return [self._row(r) for r in rows]

    def get_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        """Return one of this user's proposals by id, or ``None``."""
        row = self.conn.execute(
            "SELECT id, kind, payload, rationale, source, status, created_at, resolved_at "
            "FROM proposals WHERE user_id = ? AND id = ?",
            (self._uid(), proposal_id),
        ).fetchone()
        return self._row(row) if row is not None else None

    def set_proposal_status(self, proposal_id: int, status: str) -> bool:
        """Resolve a *pending* proposal to ``accepted``/``rejected``.

        Only a pending row moves (so a double-accept is a no-op), keeping the
        apply step idempotent. Returns ``True`` if a row was updated.
        """
        cur = self.conn.execute(
            "UPDATE proposals SET status = ?, resolved_at = datetime('now') "
            "WHERE user_id = ? AND id = ? AND status = 'pending'",
            (status, self._uid(), proposal_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        d = dict(row)
        try:
            d["payload"] = json.loads(d["payload"])
        except (ValueError, TypeError):
            d["payload"] = {}
        return d
