"""High-level read/write API over the behavioral memory database.

:class:`MemoryStore` is the single object the rest of Prefrontal uses to touch
the memory layer. It wraps a :class:`sqlite3.Connection` and exposes intention-
revealing methods for the three tables:

- ``episodes`` — :meth:`MemoryStore.log_episode`, :meth:`MemoryStore.recent_episodes`,
  :meth:`MemoryStore.episodes_by_type`.
- ``patterns`` — :meth:`MemoryStore.upsert_pattern`, :meth:`MemoryStore.get_patterns`.
- ``coaching_state`` — :meth:`MemoryStore.get_state`, :meth:`MemoryStore.set_state`,
  :meth:`MemoryStore.all_state`.

Rows are returned as plain ``dict``\\ s so callers never have to think about
:class:`sqlite3.Row`. Writes commit immediately — the access patterns here are
low-volume, human-paced events, so per-write commits keep the on-disk state
trustworthy without any meaningful cost.

**Multi-tenant scoping.** A store is constructed bound to a ``user_id`` (via
:meth:`MemoryStore.scoped`) and structurally injects it into every per-user
read and write — so no call site can forget a ``WHERE user_id = ?`` and leak one
user's rows to another. A store with no ``user_id`` (the default) is *unscoped*:
it may only call the user-management methods (:meth:`create_user`,
:meth:`get_user_by_token_hash`, :meth:`list_users`, …) and :meth:`each_user`;
any per-user method raises :class:`RuntimeError` rather than silently scanning
everyone's data. See ``docs/multi-tenant.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote_plus

from prefrontal.memory.db import connect, init_db

#: Allowed values for ``episodes.episode_type`` (see docs/schema.md).
EPISODE_TYPES = ("departure", "task", "checkin", "reminder", "mail")
#: Allowed values for ``episodes.outcome``.
OUTCOMES = ("success", "miss", "partial")

#: Coaching-state defaults seeded for every new user at provision time. These
#: used to live as an ``INSERT OR IGNORE`` seed block in ``schema.sql``; with
#: per-user state they are written scoped to each user by :meth:`provision_user`
#: instead, so "a fresh user looks like a fresh install" is one code path.
DEFAULT_COACHING_STATE: tuple[tuple[str, str, str], ...] = (
    ("preferred_briefing_format", "short", "explicit"),
    ("escalation_delay_minutes", "5", "inferred"),
    ("responsive_hours_start", "08:00", "inferred"),
    ("responsive_hours_end", "14:00", "inferred"),
    ("preferred_reminder_channel", "notification", "inferred"),
    ("time_estimation_bias", "1.4", "inferred"),
    ("active_escalation_path", "notification,sound,tts", "explicit"),
    # Departure-reminder tuning (see prefrontal.modules.departure). Travel time
    # is estimated locally: straight-line distance * road_factor / speed, then
    # padded by time_estimation_bias and a prep buffer.
    ("travel_speed_kmh", "30", "inferred"),
    ("travel_road_factor", "1.3", "inferred"),
    ("departure_prep_minutes", "5", "inferred"),
    ("departure_heads_up_minutes", "30", "inferred"),
    ("departure_soon_minutes", "10", "inferred"),
    # Opt-in network geocoding (Nominatim) for commitment destinations. Off by
    # default: local-first stays the default, and curated `places` + the static
    # `lead_minutes` fallback work without it. Set to '1' to allow the calendar
    # sync to resolve free-text locations to coordinates.
    ("geocoding_enabled", "0", "explicit"),
)


def sha256_hex(token: str) -> str:
    """Return the hex SHA-256 of a token — what we store and compare against.

    Tokens are high-entropy random strings (not human passwords), so a single
    SHA-256 is the right primitive: fast lookups, no plaintext at rest, and no
    need for a slow password hash. The raw token is shown once at creation and
    never stored.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """Mint a fresh, URL-safe access token (shown once, stored only as a hash)."""
    return secrets.token_urlsafe(32)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a :class:`sqlite3.Row` to a ``dict`` (or pass through ``None``)."""
    return dict(row) if row is not None else None


def _safe_close(conn: sqlite3.Connection) -> None:
    """Close a connection, swallowing errors (it may already be closed/broken)."""
    try:
        conn.close()
    except sqlite3.Error:
        pass


# Human labels for known calendar feeds. The feed a calendar commitment came
# from is encoded as the ``external_id`` prefix (``family:UID``); unknown feeds
# fall back to a title-cased slug, so new feeds work without a code change.
_FEED_LABELS = {
    "personal": "Personal",
    "work": "Work",
    "outlook": "Outlook",
    "family": "Family",
}


def feed_label(external_id: str | None) -> str | None:
    """Return a display label for the calendar feed, or ``None`` if not a feed.

    Manual commitments (no namespaced ``external_id``) return ``None``.
    """
    if not external_id or ":" not in external_id:
        return None
    slug = external_id.split(":", 1)[0]
    return _FEED_LABELS.get(slug, slug.capitalize())


def feed_slug(external_id: str | None) -> str | None:
    """Return the calendar feed's namespace slug, or ``None`` for a manual event.

    The raw ``external_id`` prefix (``work:UID`` → ``work``), unmapped and
    lower-level than :func:`feed_label`. It's the stable key a surface uses to
    look up an operator-configured calendar pill (label + color), so the pretty
    label can change without the lookup key moving.
    """
    if not external_id or ":" not in external_id:
        return None
    return external_id.split(":", 1)[0]


def commitment_url(commitment: dict[str, Any]) -> str | None:
    """Return a deeplink to a commitment's source event, or ``None``.

    Prefers an explicit ``source_url`` supplied by the sync (used verbatim, so
    it works for any provider — a Google ``htmlLink``, an Outlook event URL, a
    Gmail message link, …). Otherwise derives a best-effort link for events that
    came from Google Calendar, whose iCal UID ends ``@google.com``: the bare UID
    can't reconstruct a precise event link (Google's ``eid`` also needs the
    calendar id, which an ICS feed doesn't carry), but a title *search* reliably
    lands on the event. Providers we can't derive a link for (Outlook, iCloud)
    return ``None`` unless a ``source_url`` was provided.

    Only ``http(s)`` URLs are returned, so a stored value can be dropped into an
    ``href`` without opening a ``javascript:``-style injection.
    """
    url = (commitment.get("source_url") or "").strip()
    if url:
        return url if url.startswith(("http://", "https://")) else None
    external_id = commitment.get("external_id") or ""
    title = (commitment.get("title") or "").strip()
    if title and external_id.endswith("@google.com"):
        return "https://calendar.google.com/calendar/u/0/r/search?q=" + quote_plus(title)
    return None


def _with_calendar(d: dict[str, Any]) -> dict[str, Any]:
    """Annotate a commitment dict with calendar label/key and source ``url``.

    ``calendar`` is the human label; ``calendar_key`` is the raw feed slug the
    dashboard uses to look up an operator-configured pill (see
    :attr:`prefrontal.config.Settings.calendar_labels`).
    """
    external_id = d.get("external_id")
    d["calendar"] = feed_label(external_id)
    d["calendar_key"] = feed_slug(external_id)
    d["url"] = commitment_url(d)
    return d


class MemoryStore:
    """A high-level, dict-returning interface to the Prefrontal memory tables.

    A store operates in one of two connection modes:

    - **Single connection** (``MemoryStore(conn)``) — every method runs against
      the one connection passed in. This suits the CLI, tests, and any
      single-threaded use, and is the only mode that works with a private
      ``":memory:"`` database (which cannot be reopened by a second connection).
    - **Per-thread connections** (:meth:`threaded`) — each thread that touches
      the store lazily gets its own connection to the file database. This is
      required by the webhook server: FastAPI runs sync endpoints in a
      threadpool, and a single :class:`sqlite3.Connection` is **not** safe for
      concurrent use across threads. Sharing one connection there interleaves
      statements and returns truncated, empty, or duplicated result sets at
      random. A connection per thread keeps reads genuinely concurrent and
      correct; SQLite serializes the occasional write at the file level.

    A store created via :meth:`open` or :meth:`threaded` owns the connection(s)
    it opens; call :meth:`close` (or use :meth:`open`'s context manager) to
    release them. A store wrapping a caller-supplied connection does not close
    it.

    A store is also bound to a ``user_id`` (or ``None`` for the unscoped
    user-management store). Use :meth:`scoped` to derive a per-user store that
    shares the connection(s) but injects its ``user_id`` into every statement.
    """

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        user_id: int | None = None,
        _share_from: MemoryStore | None = None,
    ) -> None:
        """Create a store in single-connection or per-thread mode.

        Args:
            conn: An already-open connection to use for every call
                (single-connection mode). Produced by
                :func:`prefrontal.memory.db.connect` or
                :func:`prefrontal.memory.db.init_db`.
            connection_factory: A zero-argument callable that opens a fresh
                connection (per-thread mode). Called once per thread; the
                returned connection is cached for that thread's lifetime.
            user_id: The user this store is scoped to. ``None`` (the default)
                leaves it unscoped — only the user-management methods and
                :meth:`each_user` may be called; per-user methods raise.
            _share_from: Internal — when set (by :meth:`scoped`), the new store
                shares the given store's connection machinery rather than owning
                its own, so a scoped store is a cheap per-request wrapper.

        Raises:
            ValueError: If neither or both of ``conn`` and
                ``connection_factory`` are provided.
        """
        self.user_id = user_id
        if _share_from is not None:
            # Share the source store's connection state verbatim: a scoped store
            # is a lightweight view that must use the *same* per-thread
            # connections (and never close them) as the store it came from.
            self._fixed_conn = _share_from._fixed_conn
            self._factory = _share_from._factory
            self._local = _share_from._local
            self._conns_by_thread = _share_from._conns_by_thread
            self._conns_lock = _share_from._conns_lock
            self._owns_conns = False
            return
        if (conn is None) == (connection_factory is None):
            raise ValueError(
                "Provide exactly one of conn or connection_factory."
            )
        self._fixed_conn = conn
        self._factory = connection_factory
        self._local = threading.local()
        # Track factory-opened connections by the thread that owns each, so
        # close() can release them all AND so connections belonging to dead
        # worker threads can be reaped (their fds reclaimed) — see ``conn``.
        self._conns_by_thread: dict[int, sqlite3.Connection] = {}
        self._conns_lock = threading.Lock()
        self._owns_conns = True

    def scoped(self, user_id: int) -> MemoryStore:
        """Return a lightweight store bound to ``user_id``, sharing the connection.

        The returned store injects ``user_id`` into every per-user read and
        write. It shares this store's connection(s) and never closes them, so it
        is cheap to create per request (the webhook auth layer makes one per
        call). Connection lifecycle stays with the store this was derived from.
        """
        return MemoryStore(user_id=user_id, _share_from=self)

    def _uid(self) -> int:
        """Return the bound ``user_id``, or raise if the store is unscoped.

        This is the structural leak-guard: a per-user method that reaches an
        unscoped store fails loudly here rather than silently scanning every
        user's rows.
        """
        if self.user_id is None:
            raise RuntimeError(
                "store is not bound to a user — call store.scoped(user_id) first"
            )
        return self.user_id

    @property
    def conn(self) -> sqlite3.Connection:
        """The connection for the calling thread.

        In single-connection mode this is always the wrapped connection. In
        per-thread mode the connection is created lazily on first access from
        each thread and reused thereafter.
        """
        if self._factory is None:
            assert self._fixed_conn is not None  # guaranteed by __init__
            return self._fixed_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._factory()
            self._local.conn = conn
            ident = threading.get_ident()
            with self._conns_lock:
                # A recycled ident means the previous owner thread is gone; close
                # its now-orphaned connection before claiming the slot.
                stale = self._conns_by_thread.pop(ident, None)
                if stale is not None and stale is not conn:
                    _safe_close(stale)
                self._conns_by_thread[ident] = conn
                self._reap_dead_conns_locked()
        return conn

    def _reap_dead_conns_locked(self) -> None:
        """Close connections whose owning thread has exited (call under lock).

        FastAPI runs sync endpoints on a pool of worker threads that AnyIO reaps
        when idle. Without this, each reaped thread's connection — 3 fds (db +
        ``-wal`` + ``-shm``) — would leak forever, eventually exhausting the
        process fd limit and 500ing every DB endpoint. Cheap: only runs when a
        brand-new thread first opens a connection, never on the request path.
        """
        alive = {t.ident for t in threading.enumerate()}
        for ident in [i for i in self._conns_by_thread if i not in alive]:
            _safe_close(self._conns_by_thread.pop(ident))

    # -- construction helpers ------------------------------------------------

    @classmethod
    def threaded(cls, db_path: str, *, initialize: bool = True) -> MemoryStore:
        """Build a store that opens one connection per accessing thread.

        The schema is applied once up front (when ``initialize`` is true); each
        per-thread connection then simply opens the existing file database.

        Args:
            db_path: Path to the database **file**. Per-thread mode cannot use
                ``":memory:"`` — a second connection to it sees an empty,
                separate database — so a file path is required.
            initialize: If ``True`` (default), apply the schema once before any
                per-thread connection is opened.

        Returns:
            A :class:`MemoryStore` in per-thread connection mode.

        Raises:
            ValueError: If ``db_path`` is ``":memory:"``.
        """
        if db_path == ":memory:":
            raise ValueError(
                "Per-thread mode requires a file database, not ':memory:'."
            )
        if initialize:
            # Apply the schema once, then drop this bootstrap connection; the
            # per-thread connections opened by the factory inherit the file.
            init_db(db_path).close()
        return cls(connection_factory=lambda: connect(db_path))

    def close(self) -> None:
        """Close any connection(s) this store opened.

        In single-connection mode this closes the wrapped connection. In
        per-thread mode it closes every connection the factory has opened. A
        store wrapping a caller-supplied connection leaves it open (the caller
        owns it) — use :meth:`open` or :meth:`threaded` for owned lifecycles. A
        scoped store (from :meth:`scoped`) never closes anything; the store it
        was derived from owns the connections.
        """
        if self._factory is None or not self._owns_conns:
            return
        with self._conns_lock:
            for conn in self._conns_by_thread.values():
                _safe_close(conn)
            self._conns_by_thread.clear()

    @classmethod
    @contextmanager
    def open(cls, db_path: str, *, initialize: bool = True) -> Iterator[MemoryStore]:
        """Open a store as a context manager, closing the connection on exit.

        Args:
            db_path: Path to the database file (or ``":memory:"``).
            initialize: If ``True`` (default), apply the schema first so the
                tables and seed rows are guaranteed to exist.

        Yields:
            A :class:`MemoryStore` bound to a fresh connection.
        """
        conn = init_db(db_path) if initialize else connect(db_path)
        try:
            yield cls(conn)
        finally:
            conn.close()

    # -- users (operator-only; live on the unscoped store) -------------------

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

    # -- episodes ------------------------------------------------------------

    def log_episode(
        self,
        episode_type: str,
        *,
        predicted_value: float | None = None,
        actual_value: float | None = None,
        acknowledged: bool | None = None,
        channel: str | None = None,
        context: str | None = None,
        outcome: str | None = None,
        notes: str | None = None,
        timestamp: str | None = None,
    ) -> int:
        """Insert a raw outcome record and return its new id.

        Args:
            episode_type: One of :data:`EPISODE_TYPES` — the kind of interaction.
            predicted_value: What the agent estimated (e.g. minutes).
            actual_value: What actually happened.
            acknowledged: Whether the user responded to the trigger.
            channel: Delivery channel (``notification``, ``sound``, ``tts``, ``sms``).
            context: Free-text context — location, time of day, task type.
            outcome: One of :data:`OUTCOMES`.
            notes: Optional agent or user annotation.
            timestamp: Optional ISO timestamp; defaults to the DB's
                ``CURRENT_TIMESTAMP`` when omitted.

        Returns:
            The auto-incremented ``id`` of the inserted episode.
        """
        columns = [
            "user_id",
            "episode_type",
            "predicted_value",
            "actual_value",
            "acknowledged",
            "channel",
            "context",
            "outcome",
            "notes",
        ]
        values: list[Any] = [
            self._uid(),
            episode_type,
            predicted_value,
            actual_value,
            acknowledged,
            channel,
            context,
            outcome,
            notes,
        ]
        if timestamp is not None:
            columns.append("timestamp")
            values.append(timestamp)
        placeholders = ", ".join("?" for _ in columns)
        cur = self.conn.execute(
            f"INSERT INTO episodes ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_episode(self, episode_id: int) -> dict[str, Any] | None:
        """Return a single episode by id, or ``None`` if it does not exist."""
        row = self.conn.execute(
            "SELECT * FROM episodes WHERE id = ? AND user_id = ?",
            (episode_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def recent_episodes(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent episodes, newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of episode dicts ordered by ``timestamp`` descending.
        """
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE user_id = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_episodes(self) -> list[dict[str, Any]]:
        """Return every episode in chronological order.

        Used by the pattern-computation pass, which aggregates the full history.
        Volume is single-user and human-paced, so loading all rows is fine.

        Returns:
            A list of episode dicts ordered by ``timestamp`` then ``id`` ascending.
        """
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE user_id = ? ORDER BY timestamp ASC, id ASC",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def episodes_since(self, since: str) -> list[dict[str, Any]]:
        """Return episodes at or after a UTC timestamp, newest first.

        Args:
            since: UTC timestamp (``YYYY-MM-DD HH:MM:SS``); inclusive lower bound.

        Returns:
            A list of episode dicts. Used by the morning briefing's "what slipped
            recently" section.
        """
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE user_id = ? AND timestamp >= ? "
            "ORDER BY timestamp DESC, id DESC",
            (self._uid(), since),
        ).fetchall()
        return [dict(r) for r in rows]

    def episodes_by_type(
        self, episode_type: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return recent episodes of a single ``episode_type``, newest first.

        Args:
            episode_type: One of :data:`EPISODE_TYPES`.
            limit: Maximum number of rows to return.

        Returns:
            A list of matching episode dicts.
        """
        rows = self.conn.execute(
            "SELECT * FROM episodes WHERE user_id = ? AND episode_type = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT ?",
            (self._uid(), episode_type, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- patterns ------------------------------------------------------------

    def upsert_pattern(
        self,
        pattern_type: str,
        context_key: str,
        *,
        observed_value: float | None = None,
        predicted_value: float | None = None,
        variance: float | None = None,
        sample_size: int = 0,
        confidence: float = 0.0,
    ) -> int:
        """Insert or update the derived pattern for ``(pattern_type, context_key)``.

        There is one pattern row per ``(pattern_type, context_key)`` pair (a
        unique constraint), so the summarizer can recompute and write in place.
        ``last_updated`` is refreshed to ``CURRENT_TIMESTAMP`` on every call.

        Args:
            pattern_type: e.g. ``time_estimation``, ``channel_response``,
                ``drift``, ``context_switch``.
            context_key: What the pattern applies to (e.g. ``departure``,
                ``morning``, ``work_block``).
            observed_value: Average or median observed.
            predicted_value: What was being estimated.
            variance: Difference; positive means the agent underestimated.
            sample_size: Number of episodes the pattern is derived from.
            confidence: 0.0–1.0; low until the sample size is meaningful.

        Returns:
            The ``id`` of the inserted or updated pattern row.
        """
        self.conn.execute(
            """
            INSERT INTO patterns (
                user_id, pattern_type, context_key, observed_value, predicted_value,
                variance, sample_size, confidence, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id, pattern_type, context_key) DO UPDATE SET
                observed_value  = excluded.observed_value,
                predicted_value = excluded.predicted_value,
                variance        = excluded.variance,
                sample_size     = excluded.sample_size,
                confidence      = excluded.confidence,
                last_updated    = CURRENT_TIMESTAMP
            """,
            (
                self._uid(),
                pattern_type,
                context_key,
                observed_value,
                predicted_value,
                variance,
                sample_size,
                confidence,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM patterns "
            "WHERE user_id = ? AND pattern_type = ? AND context_key = ?",
            (self._uid(), pattern_type, context_key),
        ).fetchone()
        return int(row["id"])

    def get_patterns(
        self, pattern_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Return derived patterns, optionally filtered by type.

        Args:
            pattern_type: If given, only patterns of this type are returned.

        Returns:
            A list of pattern dicts, highest ``confidence`` first.
        """
        if pattern_type is None:
            rows = self.conn.execute(
                "SELECT * FROM patterns WHERE user_id = ? "
                "ORDER BY confidence DESC, id ASC",
                (self._uid(),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM patterns WHERE user_id = ? AND pattern_type = ? "
                "ORDER BY confidence DESC, id ASC",
                (self._uid(), pattern_type),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- coaching state ------------------------------------------------------

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

    # -- last-known location -------------------------------------------------

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

    # -- profile narrative cache ---------------------------------------------

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

    # -- outings (Location-Aware Task Anchor) --------------------------------

    def start_outing(
        self,
        intention: str,
        time_window_minutes: float,
        *,
        home_lat: float | None = None,
        home_lon: float | None = None,
        departure_at: str | None = None,
    ) -> int:
        """Record a declared outing and return its id.

        Args:
            intention: The stated mission ("getting coffee").
            time_window_minutes: The stated "back in N minutes" window.
            home_lat: Optional baseline latitude.
            home_lon: Optional baseline longitude.
            departure_at: Optional ISO timestamp for the departure; defaults to
                the DB's ``CURRENT_TIMESTAMP``. Mainly useful for tests.

        Returns:
            The new outing's ``id``.
        """
        columns = ["user_id", "intention", "time_window_minutes", "home_lat", "home_lon"]
        values: list[Any] = [
            self._uid(), intention, time_window_minutes, home_lat, home_lon
        ]
        if departure_at is not None:
            columns.append("departure_at")
            values.append(departure_at)
        placeholders = ", ".join("?" for _ in columns)
        cur = self.conn.execute(
            f"INSERT INTO outings ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_outing(self, outing_id: int) -> dict[str, Any] | None:
        """Return a single outing by id, or ``None`` if it does not exist."""
        row = self.conn.execute(
            "SELECT * FROM outings WHERE id = ? AND user_id = ?",
            (outing_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def active_outings(self) -> list[dict[str, Any]]:
        """Return active outings with a computed ``elapsed_minutes`` field.

        Elapsed time is computed in SQL against ``CURRENT_TIMESTAMP`` (both
        timestamps are UTC), so callers never have to deal with timezones.

        Returns:
            A list of outing dicts, each including ``elapsed_minutes``.
        """
        rows = self.conn.execute(
            "SELECT *, (julianday('now') - julianday(departure_at)) * 1440.0 "
            "AS elapsed_minutes FROM outings WHERE user_id = ? AND status = 'active' "
            "ORDER BY id",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def most_recent_active_outing(self) -> dict[str, Any] | None:
        """Return the newest active outing (with ``elapsed_minutes``), or ``None``."""
        row = self.conn.execute(
            "SELECT *, (julianday('now') - julianday(departure_at)) * 1440.0 "
            "AS elapsed_minutes FROM outings WHERE user_id = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (self._uid(),),
        ).fetchone()
        return _row_to_dict(row)

    def set_outing_level(self, outing_id: int, level: str) -> None:
        """Record the highest escalation level that has fired for an outing.

        Args:
            outing_id: The outing to update.
            level: One of ``none``/``soft``/``firm``/``call``.
        """
        self.conn.execute(
            "UPDATE outings SET last_level = ? WHERE id = ? AND user_id = ?",
            (level, outing_id, self._uid()),
        )
        self.conn.commit()

    def close_outing(
        self, outing_id: int, status: str = "returned"
    ) -> dict[str, Any] | None:
        """Close an active outing and return it with a computed ``actual_minutes``.

        Args:
            outing_id: The outing to close.
            status: Terminal status to set (``returned`` or ``abandoned``).

        Returns:
            The closed outing dict including ``actual_minutes`` (minutes between
            departure and return), or ``None`` if the outing was not active.
        """
        cur = self.conn.execute(
            "UPDATE outings SET status = ?, returned_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (status, outing_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        row = self.conn.execute(
            "SELECT *, (julianday(returned_at) - julianday(departure_at)) * 1440.0 "
            "AS actual_minutes FROM outings WHERE id = ? AND user_id = ?",
            (outing_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def recent_outings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent outings (any status), newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of outing dicts ordered by ``id`` descending.
        """
        rows = self.conn.execute(
            "SELECT * FROM outings WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- focus sessions (Hyperfocus) -----------------------------------------

    def start_focus_session(
        self,
        intended_task: str,
        *,
        planned_minutes: float | None = None,
        aligned: bool = True,
        started_at: str | None = None,
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

        Returns:
            The new session's ``id``.
        """
        columns = ["user_id", "intended_task", "planned_minutes", "aligned"]
        values: list[Any] = [
            self._uid(), intended_task, planned_minutes, 1 if aligned else 0
        ]
        if started_at is not None:
            columns.append("started_at")
            values.append(started_at)
        placeholders = ", ".join("?" for _ in columns)
        cur = self.conn.execute(
            f"INSERT INTO focus_sessions ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_focus_session(self, session_id: int) -> dict[str, Any] | None:
        """Return a single focus session by id, or ``None`` if it does not exist."""
        row = self.conn.execute(
            "SELECT * FROM focus_sessions WHERE id = ? AND user_id = ?",
            (session_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def active_focus_sessions(self) -> list[dict[str, Any]]:
        """Return active focus sessions with a computed ``elapsed_minutes`` field.

        Elapsed time is computed in SQL against ``CURRENT_TIMESTAMP`` (both
        timestamps are UTC), so callers never have to deal with timezones.

        Returns:
            A list of session dicts, each including ``elapsed_minutes``.
        """
        rows = self.conn.execute(
            "SELECT *, (julianday('now') - julianday(started_at)) * 1440.0 "
            "AS elapsed_minutes FROM focus_sessions "
            "WHERE user_id = ? AND status = 'active' ORDER BY id",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def most_recent_active_focus_session(self) -> dict[str, Any] | None:
        """Return the newest active focus session (with ``elapsed_minutes``), or ``None``."""
        row = self.conn.execute(
            "SELECT *, (julianday('now') - julianday(started_at)) * 1440.0 "
            "AS elapsed_minutes FROM focus_sessions "
            "WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (self._uid(),),
        ).fetchone()
        return _row_to_dict(row)

    def set_focus_session_level(self, session_id: int, level: str) -> None:
        """Record the highest interrupt level that has fired for a session.

        Args:
            session_id: The session to update.
            level: One of ``none``/``check``/``break``.
        """
        self.conn.execute(
            "UPDATE focus_sessions SET last_level = ? WHERE id = ? AND user_id = ?",
            (level, session_id, self._uid()),
        )
        self.conn.commit()

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
        cur = self.conn.execute(
            "UPDATE focus_sessions SET status = ?, ended_at = CURRENT_TIMESTAMP, "
            "breadcrumb = COALESCE(?, breadcrumb), outcome = COALESCE(?, outcome) "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (status, breadcrumb, outcome, session_id, self._uid()),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        row = self.conn.execute(
            "SELECT *, (julianday(ended_at) - julianday(started_at)) * 1440.0 "
            "AS actual_minutes FROM focus_sessions WHERE id = ? AND user_id = ?",
            (session_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def recent_focus_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent focus sessions (any status), newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of session dicts ordered by ``id`` descending.
        """
        rows = self.conn.execute(
            "SELECT * FROM focus_sessions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- commitments (schedule for impact analysis) --------------------------

    def upsert_commitment(
        self,
        *,
        title: str,
        start_at: str,
        external_id: str | None = None,
        end_at: str | None = None,
        location: str | None = None,
        source_url: str | None = None,
        dest_lat: float | None = None,
        dest_lon: float | None = None,
        lead_minutes: float = 10.0,
        hardness: str = "soft",
        source: str = "calendar",
        kind: str = "self",
        kind_source: str | None = None,
    ) -> tuple[int, bool]:
        """Insert or update a commitment, returning ``(id, created)``.

        When ``external_id`` is given and already exists, the row is updated in
        place (and re-activated) — so re-syncing a calendar is idempotent.
        Timestamps should already be normalized to UTC (see
        :func:`prefrontal.commitments.to_utc`).

        Args:
            title: Commitment title.
            start_at: UTC start timestamp (``YYYY-MM-DD HH:MM:SS``).
            external_id: Calendar event id, or ``None`` for a manual entry.
            end_at: Optional UTC end timestamp.
            location: Optional free-text location.
            source_url: Optional deeplink to the source event/email (stored
                verbatim and surfaced in the dashboard).
            dest_lat: Optional destination latitude (enables travel-time
                estimation for departure reminders).
            dest_lon: Optional destination longitude.
            lead_minutes: Travel+prep buffer needed before ``start_at``.
            hardness: ``hard`` or ``soft``.
            source: ``calendar`` or ``manual``.

        Returns:
            ``(id, created)`` where ``created`` is ``True`` for a new row.
        """
        if external_id is not None:
            existing = self.conn.execute(
                "SELECT id FROM commitments WHERE user_id = ? AND external_id = ?",
                (self._uid(), external_id),
            ).fetchone()
            if existing is not None:
                self.conn.execute(
                    "UPDATE commitments SET title = ?, start_at = ?, end_at = ?, "
                    "location = ?, source_url = ?, dest_lat = ?, dest_lon = ?, "
                    "lead_minutes = ?, hardness = ?, source = ?, kind = ?, "
                    "kind_source = ?, status = 'active', "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND user_id = ?",
                    (title, start_at, end_at, location, source_url, dest_lat,
                     dest_lon, lead_minutes, hardness, source, kind, kind_source,
                     existing["id"], self._uid()),
                )
                self.conn.commit()
                return int(existing["id"]), False

        cur = self.conn.execute(
            "INSERT INTO commitments (user_id, external_id, title, start_at, end_at, "
            "location, source_url, dest_lat, dest_lon, lead_minutes, hardness, "
            "source, kind, kind_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._uid(), external_id, title, start_at, end_at, location, source_url,
             dest_lat, dest_lon, lead_minutes, hardness, source, kind, kind_source),
        )
        self.conn.commit()
        return int(cur.lastrowid), True

    def get_commitment(self, commitment_id: int) -> dict[str, Any] | None:
        """Return a single commitment by id, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM commitments WHERE id = ? AND user_id = ?",
            (commitment_id, self._uid()),
        ).fetchone()
        d = _row_to_dict(row)
        return _with_calendar(d) if d is not None else None

    def upcoming_commitments(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return active commitments starting now or later, soonest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of commitment dicts ordered by ``start_at`` ascending.
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            "AND start_at >= datetime('now') ORDER BY start_at ASC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [_with_calendar(dict(r)) for r in rows]

    def commitments_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Return active commitments starting in ``[start, end)``, soonest first.

        Args:
            start: Inclusive UTC lower bound (``YYYY-MM-DD HH:MM:SS``).
            end: Exclusive UTC upper bound.

        Returns:
            A list of commitment dicts (e.g. "today's" commitments for the briefing).
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            "AND start_at >= ? AND start_at < ? ORDER BY start_at ASC",
            (self._uid(), start, end),
        ).fetchall()
        return [_with_calendar(dict(r)) for r in rows]

    def cancel_commitment(self, commitment_id: int) -> bool:
        """Mark a commitment cancelled. Returns ``True`` if a row changed."""
        cur = self.conn.execute(
            "UPDATE commitments SET status = 'cancelled', "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status = 'active'",
            (commitment_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def kinds_by_external_id(
        self, external_ids: set[str]
    ) -> dict[str, tuple[str, str | None]]:
        """Return ``{external_id: (kind, kind_source)}`` for the given ids.

        Used by the sync to reuse an already-decided ``kind`` (so a recurring
        event isn't re-classified every poll, and a user's correction is never
        clobbered by a fresh LLM verdict). Ids absent from the table are simply
        missing from the result.
        """
        if not external_ids:
            return {}
        ids = list(external_ids)
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT external_id, kind, kind_source FROM commitments "
            f"WHERE user_id = ? AND external_id IN ({placeholders})",
            [self._uid(), *ids],
        ).fetchall()
        return {r["external_id"]: (r["kind"], r["kind_source"]) for r in rows}

    def set_commitment_kind(
        self, commitment_id: int, kind: str, source: str
    ) -> dict[str, Any] | None:
        """Set a commitment's ``kind`` (and how it was set); return the updated row.

        Returns ``None`` if no such commitment exists.
        """
        self.conn.execute(
            "UPDATE commitments SET kind = ?, kind_source = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (kind, source, commitment_id, self._uid()),
        )
        self.conn.commit()
        return self.get_commitment(commitment_id)

    def record_kind_feedback(
        self, title: str, kind: str, *, llm_kind: str | None = None
    ) -> None:
        """Record a ``self``/``fyi`` label for a title (latest verdict wins).

        Keyed by the normalized (lowercased, trimmed) title so repeated
        corrections to the same event collapse to one row. These rows seed the
        classifier's few-shot examples (see :func:`prefrontal.classify`).
        """
        display = (title or "").strip()
        norm = display.lower()
        if not norm:
            return
        self.conn.execute(
            "INSERT INTO kind_feedback (user_id, title, display, kind, llm_kind) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, title) DO UPDATE SET display = excluded.display, "
            "kind = excluded.kind, llm_kind = excluded.llm_kind, "
            "updated_at = CURRENT_TIMESTAMP",
            (self._uid(), norm, display, kind, llm_kind),
        )
        self.conn.commit()

    def kind_feedback_examples(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return learned kind labels, most-recently-corrected first.

        Folded into the classifier prompt as few-shot examples so the model's
        verdicts evolve toward the user's corrections.
        """
        rows = self.conn.execute(
            "SELECT title, display, kind, llm_kind FROM kind_feedback "
            "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_commitment_coords(
        self, commitment_id: int, lat: float, lon: float
    ) -> bool:
        """Set a commitment's destination coordinates. ``True`` if a row changed.

        Used by the geocoding enrichment pass to fill ``dest_lat``/``dest_lon``
        on a commitment whose location was resolved to a point.
        """
        cur = self.conn.execute(
            "UPDATE commitments SET dest_lat = ?, dest_lon = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (lat, lon, commitment_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def commitments_needing_geocode(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return active upcoming commitments that have a location but no coords.

        These are the candidates for the geocoding enrichment pass: a free-text
        ``location`` is present but ``dest_lat``/``dest_lon`` are still unset.

        Args:
            limit: Maximum number of rows to return (bounds work per pass).

        Returns:
            A list of commitment dicts ordered by ``start_at`` ascending (soonest
            first), so the most imminent commitments get coordinates first.
        """
        rows = self.conn.execute(
            "SELECT * FROM commitments WHERE user_id = ? AND status = 'active' "
            "AND start_at >= datetime('now') AND location IS NOT NULL "
            "AND location != '' AND dest_lat IS NULL "
            "ORDER BY start_at ASC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- places (curated destination aliases) --------------------------------

    def add_place(
        self, name: str, lat: float, lon: float, *, label: str | None = None
    ) -> int:
        """Insert or replace a curated place alias, returning its id.

        ``name`` is the normalized match key (unique); re-adding the same name
        updates its coordinates in place.

        Args:
            name: Normalized match key (e.g. ``"gym"``).
            lat: Latitude in degrees.
            lon: Longitude in degrees.
            label: Optional original spelling for display.

        Returns:
            The place's ``id``.
        """
        self.conn.execute(
            "INSERT INTO places (user_id, name, label, lat, lon) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, name) DO UPDATE SET label = excluded.label, "
            "lat = excluded.lat, lon = excluded.lon",
            (self._uid(), name, label, lat, lon),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM places WHERE user_id = ? AND name = ?",
            (self._uid(), name),
        ).fetchone()
        return int(row["id"])

    def places(self) -> list[dict[str, Any]]:
        """Return all curated places, longest name first.

        Longest-first ordering lets a matcher prefer the most specific alias
        (e.g. ``"dentist office"`` before ``"office"``).
        """
        rows = self.conn.execute(
            "SELECT * FROM places WHERE user_id = ? "
            "ORDER BY length(name) DESC, name ASC",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- geocode cache -------------------------------------------------------

    def get_geocode_cache(self, query: str) -> dict[str, Any] | None:
        """Return a cached geocode row for ``query``, or ``None`` if not cached.

        A returned row may have ``lat``/``lon`` of ``None`` — a recorded *miss*
        (the geocoder was asked and found nothing), distinct from "never asked"
        (``None`` return).
        """
        row = self.conn.execute(
            "SELECT * FROM geocode_cache WHERE query = ?", (query,)
        ).fetchone()
        return _row_to_dict(row)

    def set_geocode_cache(
        self, query: str, lat: float | None, lon: float | None
    ) -> None:
        """Cache a geocode result for ``query`` (``lat``/``lon`` ``None`` = miss)."""
        self.conn.execute(
            "INSERT INTO geocode_cache (query, lat, lon, last_updated) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT (query) DO UPDATE SET lat = excluded.lat, "
            "lon = excluded.lon, last_updated = CURRENT_TIMESTAMP",
            (query, lat, lon),
        )
        self.conn.commit()

    # -- todos (open loops fitted into free time) ----------------------------

    def add_todo(
        self,
        title: str,
        *,
        notes: str | None = None,
        estimate_minutes: float | None = None,
        priority: int = 1,
        deadline: str | None = None,
        energy: str | None = None,
        source: str = "manual",
    ) -> int:
        """Insert an open todo and return its id.

        Args:
            title: What needs doing.
            notes: Optional detail.
            estimate_minutes: How long it'll take (enables fitting into windows).
            priority: 0 low / 1 normal / 2 high / 3 urgent.
            deadline: Optional UTC deadline (``YYYY-MM-DD HH:MM:SS``).
            energy: Optional ``low``/``medium``/``high`` hint.
            source: Where the todo came from — ``manual`` or ``impulse`` (a
                captured-and-deferred impulse). Lets surfaces distinguish the
                impulse inbox from deliberately-added loops.

        Returns:
            The new todo's id.
        """
        cur = self.conn.execute(
            "INSERT INTO todos (user_id, title, notes, estimate_minutes, priority, "
            "deadline, energy, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self._uid(), title, notes, estimate_minutes, priority, deadline, energy, source),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_todo(self, todo_id: int) -> dict[str, Any] | None:
        """Return a single todo by id, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM todos WHERE id = ? AND user_id = ?",
            (todo_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def open_todos(self) -> list[dict[str, Any]]:
        """Return open todos, highest priority then soonest deadline first.

        Returns:
            A list of todo dicts with ``status = 'open'``.
        """
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE user_id = ? AND status = 'open' "
            "ORDER BY priority DESC, (deadline IS NULL), deadline ASC, id ASC",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_todo_deadline(self, todo_id: int, deadline: str | None) -> bool:
        """Set (or clear) an open todo's deadline. Returns ``True`` if it changed.

        Plans drift; a deadline set when the todo was created — or inferred from
        its title — often needs to move. Only open todos are editable (a closed
        todo's deadline is moot), so this no-ops on a done/dropped/absent todo.

        Args:
            todo_id: The todo to update.
            deadline: A UTC deadline (``YYYY-MM-DD HH:MM:SS``), or ``None`` to clear it.
        """
        cur = self.conn.execute(
            "UPDATE todos SET deadline = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ? AND status = 'open'",
            (deadline, todo_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def close_todo(self, todo_id: int, status: str = "done") -> bool:
        """Mark a todo ``done`` or ``dropped``. Returns ``True`` if it changed.

        Args:
            todo_id: The todo to close.
            status: ``done`` or ``dropped``.
        """
        completed = "CURRENT_TIMESTAMP" if status == "done" else "NULL"
        cur = self.conn.execute(
            f"UPDATE todos SET status = ?, completed_at = {completed}, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ? AND status = 'open'",
            (status, todo_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def cancel_missing_calendar(self, keep_external_ids: set[str]) -> int:
        """Cancel future calendar commitments absent from a fresh sync.

        Manual commitments are never touched. Pruning is **feed-aware**: an
        ``external_id`` may be namespaced ``feed:id`` (e.g. ``personal:…``,
        ``work:…``), and only commitments whose namespace appears in this batch
        are eligible for cancellation. That way syncing one calendar never
        cancels another calendar's events. If the batch uses no namespaces, the
        legacy behavior applies (prune any missing calendar commitment).

        Args:
            keep_external_ids: The ``external_id``\\ s present in the new sync.

        Returns:
            The number of commitments cancelled.
        """
        keep = set(keep_external_ids)
        namespaces = {e.split(":", 1)[0] for e in keep if ":" in e}
        rows = self.conn.execute(
            "SELECT id, external_id FROM commitments "
            "WHERE user_id = ? AND source = 'calendar' "
            "AND status = 'active' AND start_at >= datetime('now')",
            (self._uid(),),
        ).fetchall()
        cancelled = 0
        for row in rows:
            eid = row["external_id"]
            if eid in keep:
                continue
            if namespaces:
                ns = eid.split(":", 1)[0] if eid and ":" in eid else None
                if ns not in namespaces:
                    continue  # belongs to a feed not part of this sync; leave it
            self.conn.execute(
                "UPDATE commitments SET status = 'cancelled', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
                (row["id"], self._uid()),
            )
            cancelled += 1
        self.conn.commit()
        return cancelled

    # -- Dismissed possible-conflicts ----------------------------------------

    def dismiss_conflict(self, signature: str) -> None:
        """Record that the user dismissed a possible-conflict pair (idempotent)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO dismissed_conflicts (user_id, signature) "
            "VALUES (?, ?)",
            (self._uid(), signature),
        )
        self.conn.commit()

    def dismissed_conflicts(self) -> set[str]:
        """Return the set of dismissed possible-conflict signatures."""
        rows = self.conn.execute(
            "SELECT signature FROM dismissed_conflicts WHERE user_id = ?",
            (self._uid(),),
        ).fetchall()
        return {r["signature"] for r in rows}

    # -- mail (ingested + triaged email) -------------------------------------

    def seen_mail_ids(self, account: str | None = None) -> set[str]:
        """Return the ``message_id``\\ s already ingested, for dedup.

        Args:
            account: If given, scope to one account's messages; otherwise return
                ids across all accounts. Dedup is account-scoped (the unique
                constraint is on ``(account, message_id)``), so callers ingesting
                one account should pass it.

        Returns:
            A set of ``message_id`` strings.
        """
        if account is None:
            rows = self.conn.execute(
                "SELECT message_id FROM mail_messages WHERE user_id = ?",
                (self._uid(),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT message_id FROM mail_messages WHERE user_id = ? AND account = ?",
                (self._uid(), account),
            ).fetchall()
        return {r["message_id"] for r in rows}

    def record_mail(
        self,
        *,
        account: str,
        message_id: str,
        policy: str = "full",
        thread_id: str | None = None,
        sender_name: str | None = None,
        sender_email: str | None = None,
        subject: str | None = None,
        received_at: str | None = None,
        snippet: str | None = None,
        body: str | None = None,
        unread: bool | None = None,
        needs_action: bool = False,
        urgency: str | None = None,
        category: str | None = None,
        waiting_on: str | None = None,
        summary: str | None = None,
        triage_source: str | None = None,
        todo_id: int | None = None,
    ) -> int:
        """Insert a triaged message and return its row id.

        Dedup is the caller's responsibility (see :meth:`seen_mail_ids`); a
        duplicate ``(account, message_id)`` raises ``sqlite3.IntegrityError``.
        Body/snippet should already have been dropped for a ``signals`` account
        by the normalizer — this method stores exactly what it is given.

        Returns:
            The new ``mail_messages`` row id.
        """
        cur = self.conn.execute(
            "INSERT INTO mail_messages ("
            "user_id, account, message_id, thread_id, sender_name, sender_email, "
            "subject, received_at, snippet, body, unread, needs_action, urgency, "
            "category, waiting_on, summary, triage_source, policy, todo_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._uid(), account, message_id, thread_id, sender_name,
                sender_email, subject, received_at, snippet, body, unread,
                needs_action, urgency, category, waiting_on, summary,
                triage_source, policy, todo_id,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_mail(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recently ingested messages, newest first.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            A list of ``mail_messages`` dicts ordered by ``received_at`` (then
            ``id``) descending.
        """
        rows = self.conn.execute(
            "SELECT * FROM mail_messages WHERE user_id = ? "
            "ORDER BY (received_at IS NULL), received_at DESC, id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mail_needing_action(self) -> list[dict[str, Any]]:
        """Return ingested messages still flagged ``needs_action``, newest first.

        A message stays here until the linked todo is closed; messages whose
        ``todo_id`` todo is no longer open are excluded, so resolving the open
        loop clears the mail from the action list.

        Returns:
            A list of ``mail_messages`` dicts.
        """
        rows = self.conn.execute(
            "SELECT m.* FROM mail_messages m "
            "LEFT JOIN todos t ON m.todo_id = t.id "
            "WHERE m.user_id = ? AND m.needs_action = 1 "
            "AND (m.todo_id IS NULL OR t.status = 'open') "
            "ORDER BY (m.received_at IS NULL), m.received_at DESC, m.id DESC",
            (self._uid(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def mail_by_todo(self, todo_id: int) -> dict[str, Any] | None:
        """Return the mail message that created ``todo_id``, or ``None``.

        The reverse of the ``mail_messages.todo_id`` link: given a todo, recover
        the email that spawned it. Used when a todo is dropped, to tell whether
        it was an intake todo (and so a triage correction) versus a manual one.
        """
        row = self.conn.execute(
            "SELECT * FROM mail_messages WHERE todo_id = ? AND user_id = ?",
            (todo_id, self._uid()),
        ).fetchone()
        return _row_to_dict(row)

    def mail_accounts_for_todos(self, todo_ids: list[int]) -> dict[int, str]:
        """Map each todo id to the mail account that created it (batch).

        The account is authoritative via the ``mail_messages.todo_id`` link — the
        same link :meth:`mail_by_todo` uses — so a surface can label a todo with
        the inbox it came from without re-parsing the todo's notes. Todos with no
        originating mail (manual/impulse) are simply absent from the result.

        Args:
            todo_ids: The todo ids to look up (empty is fine).

        Returns:
            A ``{todo_id: account}`` dict covering only mail-created todos.
        """
        if not todo_ids:
            return {}
        placeholders = ",".join("?" * len(todo_ids))
        rows = self.conn.execute(
            f"SELECT todo_id, account FROM mail_messages "
            f"WHERE user_id = ? AND todo_id IN ({placeholders})",
            (self._uid(), *todo_ids),
        ).fetchall()
        return {r["todo_id"]: r["account"] for r in rows if r["todo_id"] is not None}

    # -- Triage feedback (learned from dropped intake todos) -----------------

    def record_triage_drop(
        self,
        *,
        todo_id: int | None,
        message_id: str | None,
        sender_email: str | None,
        sender_name: str | None,
        subject: str | None,
        summary: str | None,
        category: str | None,
        urgency: str | None,
        days_open: float | None,
    ) -> int:
        """Record that the user dropped an intake-created todo (one row per drop).

        Stores the originating email's context — sender, subject, the triage
        verdict it got, and how long the todo sat open before being dropped — so
        :func:`prefrontal.mail.feedback.learned_corrections` can later separate a
        genuine false-positive (quick or repeated) from an avoidance drop.

        Returns:
            The new ``triage_feedback`` row id.
        """
        cur = self.conn.execute(
            "INSERT INTO triage_feedback ("
            "user_id, todo_id, message_id, sender_email, sender_name, subject, "
            "summary, category, urgency, days_open"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._uid(), todo_id, message_id, sender_email, sender_name,
                subject, summary, category, urgency, days_open,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def triage_dropped_senders(
        self, *, min_count: int = 2, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Senders whose intake todos the user has dropped ``min_count``+ times.

        Repetition is the reliable signal: dropping mail from the same sender
        again and again means that sender's mail rarely needs action (a real
        person you keep ignoring is rare — it's almost always a semi-automated
        sender that slipped past triage). Returned most-dropped first.
        """
        rows = self.conn.execute(
            "SELECT sender_email, MAX(sender_name) AS sender_name, "
            "COUNT(*) AS drops FROM triage_feedback "
            "WHERE user_id = ? AND sender_email IS NOT NULL AND sender_email != '' "
            "GROUP BY sender_email HAVING COUNT(*) >= ? "
            "ORDER BY drops DESC, MAX(created_at) DESC LIMIT ?",
            (self._uid(), min_count, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def triage_recent_quick_drops(
        self, *, max_days: float = 2.0, limit: int = 6
    ) -> list[dict[str, Any]]:
        """Recent drops that happened *quickly* after the todo was created.

        A todo dropped soon after it arrived (before it had time to be avoided)
        is the cleanest single-occurrence false-positive signal. Drops with an
        unknown age are excluded (we can't tell quick from avoided). Newest first.
        """
        rows = self.conn.execute(
            "SELECT sender_email, sender_name, subject, summary, category, urgency "
            "FROM triage_feedback "
            "WHERE user_id = ? AND days_open IS NOT NULL AND days_open <= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (self._uid(), max_days, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def triage_feedback_list(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recorded drop corrections, newest first (for inspection/curation)."""
        rows = self.conn.execute(
            "SELECT id, todo_id, sender_email, sender_name, subject, summary, "
            "category, urgency, days_open, created_at FROM triage_feedback "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def forget_triage_feedback(self, feedback_id: int) -> bool:
        """Delete one drop correction. Returns ``True`` if a row was removed."""
        cur = self.conn.execute(
            "DELETE FROM triage_feedback WHERE id = ? AND user_id = ?",
            (feedback_id, self._uid()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def clear_triage_feedback(self) -> int:
        """Delete all of this user's drop corrections. Returns how many were removed."""
        cur = self.conn.execute(
            "DELETE FROM triage_feedback WHERE user_id = ?",
            (self._uid(),),
        )
        self.conn.commit()
        return cur.rowcount

    # -- Nudge log (what the system last told the user) ----------------------

    def record_nudge(self, *, kind: str, message: str, level: str | None = None) -> int:
        """Record a fired nudge and return its id.

        Called by the escalation checks when they decide to nudge (``fire``),
        so every surface can show what Prefrontal last said. Purely a log — it
        has no effect on escalation state (which lives on the outing/coaching
        row); a failure to record must never block the nudge itself.

        Args:
            kind: ``"outing"`` or ``"departure"``.
            message: The delivered nudge text.
            level: The escalation level at fire time (kind-specific), if any.

        Returns:
            The new nudge row's id.
        """
        cur = self.conn.execute(
            "INSERT INTO nudges (user_id, kind, level, message) VALUES (?, ?, ?, ?)",
            (self._uid(), kind, level, message),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_nudges(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return this user's most recently sent nudges, newest first.

        Args:
            limit: Maximum number of nudges to return.

        Returns:
            A list of nudge dicts (``kind``, ``level``, ``message``,
            ``created_at``), newest first.
        """
        rows = self.conn.execute(
            "SELECT id, kind, level, message, created_at FROM nudges "
            "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (self._uid(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Task decompositions -------------------------------------------------
    #
    # ``todo_decompositions`` has no ``user_id`` of its own — it hangs off
    # ``todos`` (ON DELETE CASCADE). It is scoped *through* its parent todo: each
    # method first checks the todo belongs to this user (:meth:`_owns_todo`), so
    # one user can never read or edit another user's decomposition by id.

    def _owns_todo(self, todo_id: int) -> bool:
        """Return whether ``todo_id`` belongs to this scoped user."""
        row = self.conn.execute(
            "SELECT 1 FROM todos WHERE id = ? AND user_id = ?",
            (todo_id, self._uid()),
        ).fetchone()
        return row is not None

    def set_decomposition(
        self,
        todo_id: int,
        *,
        first_step: str,
        first_step_minutes: float | None,
        steps: list[str],
        source: str,
    ) -> None:
        """Store (or replace) a todo's decomposition. ``steps`` is JSON-encoded.

        Replacing a decomposition leaves ``done_steps`` unset (NULL), which resets
        any per-step progress — the steps themselves changed, so old check-offs no
        longer apply. No-ops if the todo is not this user's.
        """
        if not self._owns_todo(todo_id):
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO todo_decompositions "
            "(todo_id, first_step, first_step_minutes, steps, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (todo_id, first_step, first_step_minutes, json.dumps(steps), source),
        )
        self.conn.commit()

    def get_decomposition(self, todo_id: int) -> dict[str, Any] | None:
        """Return a todo's decomposition, or ``None``.

        ``steps`` is decoded to a list of strings and ``done_steps`` to a sorted
        list of completed step indices (0 = ``first_step``, 1..N = ``steps``).
        Returns ``None`` if the todo is not this user's.
        """
        if not self._owns_todo(todo_id):
            return None
        row = self.conn.execute(
            "SELECT first_step, first_step_minutes, steps, source, done_steps "
            "FROM todo_decompositions WHERE todo_id = ?",
            (todo_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["steps"] = json.loads(d["steps"]) if d["steps"] else []
        except (ValueError, TypeError):
            d["steps"] = []
        d["done_steps"] = self._decode_done_steps(d.get("done_steps"))
        return d

    @staticmethod
    def _decode_done_steps(raw: Any) -> list[int]:
        """Decode the stored ``done_steps`` JSON into a sorted list of ints."""
        try:
            value = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            return []
        if not isinstance(value, list):
            return []
        return sorted({int(i) for i in value if isinstance(i, int) and not isinstance(i, bool)})

    def set_step_done(self, todo_id: int, step_index: int, done: bool = True) -> bool:
        """Mark one decomposed step done (or undone). Returns ``True`` if valid.

        Steps are indexed with ``0`` = ``first_step`` and ``1..N`` = the remaining
        ``steps``, so a decomposition with M remaining steps has indices ``0..M``.
        Ticking a step off is its own small win — visible progress is what keeps a
        broken-down task moving. No-ops (returns ``False``) when the todo has no
        decomposition or ``step_index`` is out of range.

        Args:
            todo_id: The todo whose decomposition to update.
            step_index: Which step (``0`` = first step).
            done: ``True`` to mark done, ``False`` to clear it.
        """
        if not self._owns_todo(todo_id):
            return False
        row = self.conn.execute(
            "SELECT steps, done_steps FROM todo_decompositions WHERE todo_id = ?",
            (todo_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            steps = json.loads(row["steps"]) if row["steps"] else []
        except (ValueError, TypeError):
            steps = []
        total = 1 + (len(steps) if isinstance(steps, list) else 0)
        if step_index < 0 or step_index >= total:
            return False
        done_set = set(self._decode_done_steps(row["done_steps"]))
        if done:
            done_set.add(step_index)
        else:
            done_set.discard(step_index)
        self.conn.execute(
            "UPDATE todo_decompositions SET done_steps = ? WHERE todo_id = ?",
            (json.dumps(sorted(done_set)), todo_id),
        )
        self.conn.commit()
        return True


def seed_user_state(store: MemoryStore) -> None:
    """Seed a (scoped) store's user with the default coaching state + module defaults.

    Writes :data:`DEFAULT_COACHING_STATE` and each enabled module's
    ``default_state`` (via ``Module.seed``) without clobbering any value already
    present — so calling it twice is safe. This is the single "a fresh user looks
    like a fresh install" code path that replaces the old global schema seeds.

    Args:
        store: A store **already scoped** to the user to seed.
    """
    existing = store.all_state()
    for key, value, source in DEFAULT_COACHING_STATE:
        if key not in existing:
            store.set_state(key, value, source=source)
    # Each enabled module seeds its own coaching-state defaults (it calls
    # set_state on the scoped store, so the keys land under this user). Imported
    # lazily to keep the memory layer free of a hard dependency on the modules
    # package and to avoid an import cycle.
    from prefrontal.modules import enabled_modules

    for module in enabled_modules():
        module.seed(store)


def provision_user(
    store: MemoryStore,
    handle: str,
    *,
    display_name: str | None = None,
    token: str | None = None,
    is_operator: bool = False,
) -> tuple[dict[str, Any], str]:
    """Create a user and seed their coaching state, returning ``(user_row, token)``.

    This is the one provisioning path used by the operator CLI, the admin HTTP
    endpoints, and the single-tenant migration. It creates the user on the
    unscoped ``store`` (see :meth:`MemoryStore.create_user`), then seeds the
    per-user coaching defaults and module defaults via :func:`seed_user_state`
    on a store scoped to the new user. The raw token is returned once.

    Args:
        store: An **unscoped** store (operator context).
        handle: The new user's unique handle.
        display_name: Optional display name shown in nudges/briefings.
        token: Optional pre-chosen token (a random one is generated otherwise).
        is_operator: Whether the user may call the admin surface.

    Returns:
        ``(user_row, raw_token)`` — the token is shown once and never stored.
    """
    user, raw_token = store.create_user(
        handle,
        display_name=display_name,
        token=token,
        is_operator=is_operator,
    )
    seed_user_state(store.scoped(user["id"]))
    return user, raw_token
