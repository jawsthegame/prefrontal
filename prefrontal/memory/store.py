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

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from prefrontal.memory._helpers import (
    DEFAULT_COACHING_STATE,
    _safe_close,
    commitment_url,
    feed_label,
    feed_slug,
    generate_token,
    gmail_message_url,
    sha256_hex,
)
from prefrontal.memory.db import connect, init_db
from prefrontal.memory.repos.episodes import EpisodesRepo
from prefrontal.memory.repos.household import HouseholdRepo
from prefrontal.memory.repos.mail import MailRepo
from prefrontal.memory.repos.nudges import NudgesRepo
from prefrontal.memory.repos.patterns import PatternsRepo
from prefrontal.memory.repos.proposals import ProposalsRepo
from prefrontal.memory.repos.schedule import ScheduleRepo
from prefrontal.memory.repos.sessions import SessionsRepo
from prefrontal.memory.repos.state import StateRepo
from prefrontal.memory.repos.todos import TodosRepo
from prefrontal.memory.repos.triage import TriageRepo
from prefrontal.memory.repos.trips import TripsRepo
from prefrontal.memory.repos.users import UsersRepo

__all__ = [
    "MemoryStore",
    "feed_label",
    "feed_slug",
    "commitment_url",
    "gmail_message_url",
    "generate_token",
    "sha256_hex",
    "seed_user_state",
    "provision_user",
    "DEFAULT_COACHING_STATE",
]


class MemoryStore(
    UsersRepo,
    EpisodesRepo,
    PatternsRepo,
    StateRepo,
    SessionsRepo,
    TripsRepo,
    ScheduleRepo,
    TodosRepo,
    MailRepo,
    NudgesRepo,
    ProposalsRepo,
    HouseholdRepo,
    TriageRepo,
):
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
