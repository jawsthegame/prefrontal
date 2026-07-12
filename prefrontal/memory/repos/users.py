"""User provisioning & lookup (operator-only; unscoped store).

Mixin for :class:`prefrontal.memory.store.MemoryStore`; not used standalone.
"""
from __future__ import annotations

from typing import Any

from prefrontal.memory._helpers import (
    _row_to_dict,
    generate_token,
    sha256_hex,
)
from prefrontal.memory.repos._base import Repo


class UsersRepo(Repo):
    """User provisioning & lookup (operator-only; unscoped store)."""

    def create_user(
        self,
        handle: str,
        *,
        display_name: str | None = None,
        token: str | None = None,
        is_operator: bool = False,
        email: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Create a user, returning ``(user_row, raw_token)``.

        The raw token is returned **once** (like an API key); only its
        ``sha256`` is stored. A token is generated if none is supplied. This is
        an operator-only method and runs on the unscoped store — it does not
        seed coaching state (see :func:`provision_user`, which wraps it).

        ``email`` (optional) is the verified Google address that signs in as this
        user; it's normalized (lowercased/stripped) and must be unique.

        Raises:
            sqlite3.IntegrityError: If ``handle`` or ``email`` is already taken.
        """
        raw_token = token or generate_token()
        cur = self.conn.execute(
            "INSERT INTO users (handle, display_name, token_hash, is_operator, email) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                handle,
                display_name,
                sha256_hex(raw_token),
                1 if is_operator else 0,
                _normalize_email(email),
            ),
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

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        """Return the (active or not) user whose ``email`` matches, or ``None``.

        The email is normalized (lowercased/stripped) before lookup, matching how
        it's stored, so the Google callback can map a verified address to a user
        without a ``GOOGLE_OAUTH_ALLOWED`` env mapping. A blank email is never a
        match (the many email-less users all store ``NULL``).
        """
        normalized = _normalize_email(email)
        if not normalized:
            return None
        row = self.conn.execute(
            "SELECT * FROM users WHERE email = ?", (normalized,)
        ).fetchone()
        return _row_to_dict(row)

    def get_user_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        """Return the user whose ``token_hash`` matches, or ``None``.

        A single indexed lookup on the unique ``token_hash`` column
        (``idx_users_token``). ``token_hash`` is already a SHA-256 of the bearer
        token — a 256-bit high-entropy value, not a low-entropy secret an attacker
        could brute-force by timing a byte-at-a-time compare — so an equality match
        is appropriate here. This replaces a ``SELECT * FROM users`` + per-row
        ``hmac.compare_digest`` loop that ran on **every** authenticated request:
        O(users) instead of O(1), and its duration leaked the user count.
        """
        row = self.conn.execute(
            "SELECT * FROM users WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return _row_to_dict(row)

    def list_users(self) -> list[dict[str, Any]]:
        """Return all users (never their tokens), oldest first."""
        rows = self.conn.execute(
            "SELECT id, handle, display_name, status, is_operator, email, created_at "
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

    def set_user_email(self, handle: str, email: str | None) -> bool:
        """Set (or, with a blank ``email``, clear) a user's Google sign-in email.

        The email is normalized (lowercased/stripped) and must be unique across
        users, so a Google address maps to exactly one account. Returns ``True``
        if the user exists (and was updated), ``False`` if no such handle.

        Raises:
            ValueError: If ``email`` is already claimed by a different user.
        """
        if self.get_user(handle) is None:
            return False
        normalized = _normalize_email(email)
        if normalized:
            clash = self.get_user_by_email(normalized)
            if clash is not None and clash["handle"] != handle:
                raise ValueError(f"Email is already used by '{clash['handle']}'.")
        self.conn.execute(
            "UPDATE users SET email = ? WHERE handle = ?", (normalized, handle)
        )
        self.conn.commit()
        return True

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


def _normalize_email(email: str | None) -> str | None:
    """Lowercase/strip an email for storage & lookup; blank/``None`` -> ``None``.

    Kept in one place so the write path (``create_user``/``set_user_email``) and
    the read path (``get_user_by_email``) can't drift on casing or whitespace —
    Google returns addresses verbatim, so ``Jamie@Gmail.com`` and ``jamie@gmail.com``
    must resolve to the same user.
    """
    if email is None:
        return None
    normalized = email.strip().lower()
    return normalized or None
