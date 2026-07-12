"""Feature-usage event stream — the spine of the usage feedback loop.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.

Every time a feature is *offered* to the user (a coaching nudge fires), *engaged*
with (a one-tap action / shortcut), or *invoked* (a dashboard endpoint or CLI
command), one append-only row lands in ``feature_events`` carrying the structured
``(feature, intervention)`` key. That key already exists in memory on a coaching
:class:`~prefrontal.coaching.Cue` (``module`` + ``intervention``) but was never
persisted; the pull surfaces left no trace at all. Recording all three here — and
nowhere else — lets :mod:`prefrontal.stats` answer "what am I using, and what am
I not?" with one ``GROUP BY`` rather than scraping free-text ``episodes.context``.

Kept deliberately separate from :mod:`episodes`: this is meta-telemetry about
*which behaviors you lean on*, not a behavioral outcome to learn from. A failure
to record must never block the thing it's recording — callers wrap it so the loop
is best-effort, exactly like :meth:`NudgesRepo.record_nudge`.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory.repos._base import Repo

#: The three kinds of usage event, in the order the /stats panel reasons about
#: them: the system *offered* a push feature, the user *engaged* with one, or the
#: user *invoked* a pull feature (endpoint / CLI command).
FEATURE_EVENTS: tuple[str, ...] = ("offered", "engaged", "invoked")


class FeatureUsageRepo(Repo):
    """Append-only feature-usage events + the rollup the Insights page charts."""

    def record_feature_event(
        self,
        feature: str,
        event: str,
        *,
        intervention: str | None = None,
        source: str | None = None,
        ref: str | None = None,
    ) -> int:
        """Record one usage event and return its new id.

        Args:
            feature: The module key (``location_anchor``) for a push feature, or
                the pull-surface name (``panic``, ``briefing``) for something the
                user invoked.
            event: One of :data:`FEATURE_EVENTS` — ``offered`` / ``engaged`` /
                ``invoked``.
            intervention: The declared ``Intervention.name`` for a push feature
                (e.g. ``tiny_first_step``); ``None`` for pull surfaces.
            source: Where the event came from (``ntfy``/``shortcut``/``http``/
                ``cli``/the channel class), for cheap after-the-fact slicing.
            ref: Optional free-text hook back to the underlying thing (an entity
                id, the route path, …).

        Returns:
            The auto-incremented ``id`` of the inserted event.
        """
        cur = self.conn.execute(
            "INSERT INTO feature_events (user_id, feature, intervention, event, source, ref) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self._uid(), feature, intervention, event, source, ref),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def feature_usage_rollup(self, days: int = 30) -> list[dict[str, Any]]:
        """Per-feature usage counts over the last ``days``, for the /stats panel.

        One row per ``feature`` actually seen in the window, each with the
        ``offered`` / ``engaged`` / ``invoked`` counts and the most recent event
        timestamp. The caller (:mod:`prefrontal.stats`) joins this against the
        module registry so a *never-fired* feature — absent here entirely — still
        shows up as dormant.

        Args:
            days: Rolling window in days.

        Returns:
            A list of ``{feature, offered, engaged, invoked, last_used}`` dicts,
            most-recently-used first.
        """
        return self._query_all(
            "SELECT feature, "
            "  SUM(event = 'offered')  AS offered, "
            "  SUM(event = 'engaged')  AS engaged, "
            "  SUM(event = 'invoked')  AS invoked, "
            "  MAX(created_at)         AS last_used "
            "FROM feature_events "
            "WHERE user_id = ? AND created_at >= datetime('now', ?) "
            "GROUP BY feature "
            "ORDER BY last_used DESC",
            (self._uid(), f"-{int(days)} days"),
        )
