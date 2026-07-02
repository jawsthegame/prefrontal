"""Coaching state, last-known location, and the profile-narrative cache.

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

import hashlib
from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
)


class StateRepo:
    """Coaching state, last-known location, and the profile-narrative cache."""

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Return a coaching-state value by key.

        Args:
            key: The preference name.
            default: Value to return if the key is absent.

        Returns:
            The stored value, or ``default`` if the key does not exist.
        """
        row = self.conn.execute(
            "SELECT value FROM coaching_state WHERE user_id = ? AND key = ?",
            (self._uid(), key),
        ).fetchone()
        return row["value"] if row is not None else default

    def get_float(self, key: str, default: float) -> float:
        """Return a coaching-state value parsed as a float.

        Coaching state is stored as text, so numeric preferences are re-parsed on
        every read. This is the single typed accessor for that — a missing key or
        an unparseable value both fall back to ``default`` rather than raising, so
        a corrupt or hand-edited value can never take down a nudge path.

        Args:
            key: The preference name.
            default: Value to return if the key is absent or not a valid float.

        Returns:
            The stored value as a ``float``, or ``default``.
        """
        raw = self.get_state(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool) -> bool:
        """Return a coaching-state value parsed as a boolean.

        Recognizes ``1``/``true``/``yes``/``on`` (case-insensitive) as true and
        ``0``/``false``/``no``/``off`` as false. A missing key or any
        unrecognized value falls back to ``default``, so a typo degrades to the
        safe default rather than silently reading as false.

        Args:
            key: The preference name.
            default: Value to return if the key is absent or unrecognized.

        Returns:
            The stored value as a ``bool``, or ``default``.
        """
        raw = self.get_state(key)
        if raw is None:
            return default
        token = raw.strip().lower()
        if token in ("1", "true", "yes", "on"):
            return True
        if token in ("0", "false", "no", "off"):
            return False
        return default

    def set_state(self, key: str, value: str, source: str = "inferred") -> None:
        """Insert or update a coaching-state preference.

        Args:
            key: The preference name (unique).
            value: The value to store (stored as text).
            source: ``explicit`` if the user set it, ``inferred`` if the agent
                derived it.
        """
        self.conn.execute(
            """
            INSERT INTO coaching_state (user_id, key, value, source, last_updated)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id, key) DO UPDATE SET
                value        = excluded.value,
                source       = excluded.source,
                last_updated = CURRENT_TIMESTAMP
            """,
            (self._uid(), key, value, source),
        )
        self.conn.commit()

    def all_state(self) -> dict[str, dict[str, Any]]:
        """Return the entire coaching state keyed by preference name.

        Returns:
            A mapping of ``key`` -> the full row dict (``value``, ``source``,
            ``last_updated``, ...), convenient for the summarizer.
        """
        rows = self.conn.execute(
            "SELECT * FROM coaching_state WHERE user_id = ? ORDER BY key ASC",
            (self._uid(),),
        ).fetchall()
        return {r["key"]: dict(r) for r in rows}

    def set_location(
        self, lat: float, lon: float, accuracy_m: float | None = None
    ) -> None:
        """Record the user's last-known position (from an iOS Shortcut ping).

        Stored in ``coaching_state`` as three keys so it rides the same
        machinery as every other preference. The freshness timestamp is the
        ``last_updated`` of the latitude row (see :meth:`get_location`).

        Args:
            lat: Latitude in degrees.
            lon: Longitude in degrees.
            accuracy_m: Optional reported accuracy radius in metres.
        """
        self.set_state("last_location_lat", repr(float(lat)), source="explicit")
        self.set_state("last_location_lon", repr(float(lon)), source="explicit")
        self.set_state(
            "last_location_accuracy_m",
            "" if accuracy_m is None else repr(float(accuracy_m)),
            source="explicit",
        )

    def get_location(self) -> dict[str, Any] | None:
        """Return the last-known position, or ``None`` if none has been recorded.

        Returns:
            A dict with ``lat``, ``lon``, ``accuracy_m`` (``None`` if unreported),
            and ``at`` (the UTC timestamp it was last set), or ``None`` when no
            location has ever been pinged.
        """
        row = self.conn.execute(
            "SELECT value, last_updated FROM coaching_state "
            "WHERE user_id = ? AND key = 'last_location_lat'",
            (self._uid(),),
        ).fetchone()
        if row is None:
            return None
        lon = self.get_state("last_location_lon")
        if lon is None:
            return None
        accuracy = self.get_state("last_location_accuracy_m")
        return {
            "lat": float(row["value"]),
            "lon": float(lon),
            "accuracy_m": float(accuracy) if accuracy else None,
            "at": row["last_updated"],
        }

    def get_profile_cache(self) -> dict[str, Any] | None:
        """Return the cached profile narrative row, or ``None`` if unset.

        The row carries the served ``text`` plus provenance (``source``,
        ``model``, ``generated_at``) and the ``structured``/``structured_hash``
        the prose was derived from (for staleness checks).
        """
        row = self.conn.execute(
            "SELECT text, source, model, structured, structured_hash, "
            "generated_at FROM profile_cache WHERE user_id = ?",
            (self._uid(),),
        ).fetchone()
        return _row_to_dict(row)

    def set_profile_cache(
        self,
        text: str,
        *,
        source: str,
        model: str | None,
        structured: str,
    ) -> None:
        """Insert or replace the single cached profile narrative.

        ``structured_hash`` is computed here (a SHA-256 of ``structured``) so the
        caller never has to; ``generated_at`` is refreshed to the DB clock.

        Args:
            text: The narrative to serve from ``GET /profile``.
            source: ``llm`` if a model produced it, ``heuristic`` for the fallback.
            model: The model name when ``source == "llm"``, else ``None``.
            structured: The structured profile the narrative was derived from.
        """
        digest = hashlib.sha256(structured.encode("utf-8")).hexdigest()
        self.conn.execute(
            """
            INSERT INTO profile_cache (
                user_id, text, source, model, structured, structured_hash, generated_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                text            = excluded.text,
                source          = excluded.source,
                model           = excluded.model,
                structured      = excluded.structured,
                structured_hash = excluded.structured_hash,
                generated_at    = CURRENT_TIMESTAMP
            """,
            (self._uid(), text, source, model, structured, digest),
        )
        self.conn.commit()

    # -- escalating sessions (shared by outings & focus sessions) ------------
    #
    # An "escalating session" is a time-boxed row that escalates through severity
    # levels and eventually closes: outings (Location-Aware Task Anchor) and focus
    # sessions (Hyperfocus) are two instances of it. Their lifecycles were
    # duplicated method-for-method; these generic helpers hold the shared CRUD +
    # elapsed/actual-minutes SQL once, parameterized by table and the timestamp
    # columns. Table/column names are internal constants, never user input, so the
    # f-string interpolation is injection-safe. The public per-kind methods below
    # are thin delegations.
