"""User provisioning & lookup (operator-only; unscoped store).

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

import hmac
from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
    generate_token,
    sha256_hex,
)


class UsersRepo:
    """User provisioning & lookup (operator-only; unscoped store)."""

    def create_user(
        self,
        handle: str,
        *,
        display_name: str | None = None,
        token: str | None = None,
        is_operator: bool = False,
    ) -> tuple[dict[str, Any], str]:
        """Create a user, returning ``(user_row, raw_token)``.

        The raw token is returned **once** (like an API key); only its
        ``sha256`` is stored. A token is generated if none is supplied. This is
        an operator-only method and runs on the unscoped store — it does not
        seed coaching state (see :func:`provision_user`, which wraps it).

        Raises:
            sqlite3.IntegrityError: If ``handle`` is already taken.
        """
        raw_token = token or generate_token()
        cur = self.conn.execute(
            "INSERT INTO users (handle, display_name, token_hash, is_operator) "
            "VALUES (?, ?, ?, ?)",
            (handle, display_name, sha256_hex(raw_token), 1 if is_operator else 0),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (int(cur.lastrowid),)
        ).fetchone()
        return dict(row), raw_token

    def get_user(self, handle: str) -> dict[str, Any] | None:
        """Return a user row by ``handle``, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM users WHERE handle = ?", (handle,)
        ).fetchone()
        return _row_to_dict(row)

    def get_user_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        """Return the user whose ``token_hash`` matches, or ``None``.

        The comparison goes through the indexed lookup; callers should compare
        the *hash* with :func:`hmac.compare_digest` when they already hold a
        candidate (see the webhook auth layer) to keep it constant-time.
        """
        rows = self.conn.execute("SELECT * FROM users").fetchall()
        for row in rows:
            if hmac.compare_digest(row["token_hash"], token_hash):
                return dict(row)
        return None

    def list_users(self) -> list[dict[str, Any]]:
        """Return all users (never their tokens), oldest first."""
        rows = self.conn.execute(
            "SELECT id, handle, display_name, status, is_operator, created_at "
            "FROM users ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def each_user(self, *, status: str | None = "active") -> list[dict[str, Any]]:
        """Return users for the learning/summarizer fan-out, scoped by ``status``.

        Args:
            status: Only return users with this status (default ``active``);
                pass ``None`` for every user.
        """
        if status is None:
            rows = self.conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM users WHERE status = ? ORDER BY id ASC", (status,)
            ).fetchall()
        return [dict(r) for r in rows]

    def set_user_status(self, handle: str, status: str) -> bool:
        """Set a user's ``status`` (``active``/``disabled``). ``True`` if changed."""
        cur = self.conn.execute(
            "UPDATE users SET status = ? WHERE handle = ?", (status, handle)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def rotate_user_token(self, handle: str) -> str | None:
        """Generate and store a new token for ``handle``; return it once.

        Returns ``None`` if no such user exists. The old token stops working
        immediately (devices holding it must be re-provisioned).
        """
        if self.get_user(handle) is None:
            return None
        raw_token = generate_token()
        self.conn.execute(
            "UPDATE users SET token_hash = ? WHERE handle = ?",
            (sha256_hex(raw_token), handle),
        )
        self.conn.commit()
        return raw_token
