"""Read-only share link for a human assistant/VA (see ``va_shares`` in schema.sql).

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
Mint/status/revoke run **scoped** (the owner acts on their own link);
:meth:`resolve_va_share` runs **unscoped** — the visitor holding the link has
no Prefrontal account, so the token alone is the only identity available.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory._helpers import generate_token, sha256_hex
from prefrontal.memory.repos._base import Repo


class VaShareRepo(Repo):
    """Mint/revoke the caller's VA share link; resolve a token back to its owner."""

    def create_va_share(self) -> tuple[dict[str, Any], str]:
        """Mint a fresh VA share link, revoking any existing one first.

        Returns ``(row, raw_token)`` — the token is shown once, like a user's
        API token; only its ``sha256`` is stored. One active link per user: a
        fresh mint invalidates the old URL rather than leaving two live. The
        revoke and insert share **one commit** (not two), so a concurrent mint
        from another connection can't interleave between them and leave two
        rows active at once.
        """
        self._revoke_va_share_uncommitted()
        raw_token = generate_token()
        self.conn.execute(
            "INSERT INTO va_shares (user_id, token_hash) VALUES (?, ?)",
            (self._uid(), sha256_hex(raw_token)),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM va_shares WHERE user_id = ? AND revoked_at IS NULL",
            (self._uid(),),
        ).fetchone()
        return dict(row), raw_token

    def get_va_share(self) -> dict[str, Any] | None:
        """Return the caller's active share row (no raw token), or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM va_shares WHERE user_id = ? AND revoked_at IS NULL",
            (self._uid(),),
        ).fetchone()
        return dict(row) if row is not None else None

    def revoke_va_share(self) -> bool:
        """Revoke the caller's active share link, if any. ``True`` if one was revoked."""
        changed = self._revoke_va_share_uncommitted()
        self.conn.commit()
        return changed

    def _revoke_va_share_uncommitted(self) -> bool:
        """The revoke UPDATE without its own commit, so :meth:`create_va_share`
        can fold it into the same transaction as the insert that follows."""
        cur = self.conn.execute(
            "UPDATE va_shares SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND revoked_at IS NULL",
            (self._uid(),),
        )
        return cur.rowcount > 0

    def resolve_va_share(self, token: str) -> int | None:
        """Return the owning ``user_id`` for an active share ``token``, or ``None``.

        Unscoped — the public link carries no signed-in identity, so this is how
        an anonymous request resolves one straight to a ``user_id``.
        """
        row = self.conn.execute(
            "SELECT user_id FROM va_shares WHERE token_hash = ? AND revoked_at IS NULL",
            (sha256_hex(token),),
        ).fetchone()
        return int(row[0]) if row is not None else None
