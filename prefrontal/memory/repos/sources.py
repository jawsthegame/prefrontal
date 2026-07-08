"""Per-user external source registry (connected mailboxes + calendars).

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.

Rows hold the *sealed* secret bytes verbatim — sealing/opening lives one layer up
in :mod:`prefrontal.sources`, so this repo stays pure SQL and never touches the
encryption key. Every method is scoped to the bound user via ``self._uid()``.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory.repos._base import Repo


class SourcesRepo(Repo):
    """CRUD for a user's connected sources (IMAP mailboxes, Google calendars)."""

    def upsert_source(
        self,
        *,
        kind: str,
        account: str,
        config: str,
        secret_enc: bytes | None = None,
        enabled: bool = True,
    ) -> int:
        """Insert or update a source (unique per user+kind+account); return its id.

        Re-adding the same ``(kind, account)`` updates config/enabled in place, so
        re-connecting an account is idempotent. ``secret_enc=None`` on an *update*
        leaves any existing sealed secret untouched (a config-only edit needn't
        re-seal the secret); on a fresh insert it stores no secret.

        Args:
            kind: Connector kind (``"imap"`` | ``"gcal"``).
            account: Logical name within the kind (``"personal"``, ``"google"``).
            config: Connector-shaped JSON string (see the ``sources`` schema).
            secret_enc: Fernet-sealed secret bytes, or ``None`` to leave/omit.
            enabled: Whether the source is active.

        Returns:
            The source row id.
        """
        return self._upsert_returning_id(
            "INSERT INTO sources (user_id, kind, account, config, secret_enc, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, kind, account) DO UPDATE SET "
            "config = excluded.config, "
            "secret_enc = COALESCE(excluded.secret_enc, sources.secret_enc), "
            "enabled = excluded.enabled, updated_at = CURRENT_TIMESTAMP",
            (self._uid(), kind, account, config, secret_enc, 1 if enabled else 0),
            select_sql=(
                "SELECT id FROM sources WHERE user_id = ? AND kind = ? AND account = ?"
            ),
            select_params=(self._uid(), kind, account),
        )

    def get_source(self, kind: str, account: str) -> dict[str, Any] | None:
        """Return a single source by ``(kind, account)``, or ``None``."""
        return self._query_one(
            "SELECT * FROM sources WHERE user_id = ? AND kind = ? AND account = ?",
            (self._uid(), kind, account),
        )

    def list_sources(
        self, kind: str | None = None, *, include_disabled: bool = True
    ) -> list[dict[str, Any]]:
        """Return the user's sources, optionally filtered by kind / enabled.

        Args:
            kind: Restrict to one connector kind, or ``None`` for all.
            include_disabled: When ``False``, only ``enabled`` sources are returned.

        Returns:
            Source dicts ordered by ``(kind, account)``.
        """
        clauses = ["user_id = ?"]
        params: list[Any] = [self._uid()]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if not include_disabled:
            clauses.append("enabled = 1")
        where = " AND ".join(clauses)
        return self._query_all(
            f"SELECT * FROM sources WHERE {where} ORDER BY kind, account",
            tuple(params),
        )

    def delete_source(self, kind: str, account: str) -> bool:
        """Delete a source outright; return ``True`` if a row was removed."""
        cur = self.conn.execute(
            "DELETE FROM sources WHERE user_id = ? AND kind = ? AND account = ?",
            (self._uid(), kind, account),
        )
        self.conn.commit()
        return cur.rowcount > 0
