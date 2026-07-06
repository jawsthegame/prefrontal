"""Multi-tenant isolation, provisioning, fan-out, and migration tests.

The single highest-value guarantee here is **isolation**: two users with two
scoped stores never read or write each other's rows, and an unscoped per-user
call raises rather than scanning everyone. See ``docs/multi-tenant.md`` §11.
"""

from __future__ import annotations

import sqlite3

import pytest

from prefrontal.memory.db import connect, init_db
from prefrontal.memory.migrate import (
    MULTI_TENANT_VERSION,
    is_multi_tenant,
    migrate_to_multi_tenant,
    run_migrations,
)
from prefrontal.memory.patterns import recompute_patterns
from prefrontal.memory.store import (
    MemoryStore,
    provision_user,
    seed_user_state,
    sha256_hex,
)


@pytest.fixture()
def unscoped():
    """An unscoped, schema-initialized in-memory store (no users yet)."""
    conn = init_db(":memory:")
    try:
        yield MemoryStore(conn)
    finally:
        conn.close()


@pytest.fixture()
def two_users(unscoped):
    """Provision two users; return ``(store_a, store_b, ua, ub)``."""
    ua, _ = provision_user(unscoped, "alice", display_name="Alice")
    ub, _ = provision_user(unscoped, "bob", display_name="Bob")
    return unscoped.scoped(ua["id"]), unscoped.scoped(ub["id"]), ua, ub


# -- the unscoped guard ------------------------------------------------------


def test_unscoped_per_user_call_raises(unscoped):
    """A per-user method on an unscoped store fails loudly, not silently."""
    with pytest.raises(RuntimeError, match="not bound to a user"):
        unscoped.log_episode("task")
    with pytest.raises(RuntimeError):
        unscoped.open_todos()
    with pytest.raises(RuntimeError):
        unscoped.get_state("anything")


def test_user_crud_works_unscoped(unscoped):
    """User management lives on the unscoped store and needs no scope."""
    user, token = unscoped.create_user("carol", display_name="Carol")
    assert user["handle"] == "carol"
    # The raw token is never stored; only its hash.
    assert unscoped.get_user_by_token_hash(sha256_hex(token))["id"] == user["id"]
    assert [u["handle"] for u in unscoped.list_users()] == ["carol"]


# -- isolation across every per-user table -----------------------------------


def _write_full_object_set(store: MemoryStore, tag: str) -> None:
    """Write one of every kind of user-owned object, tagged so we can tell them apart."""
    store.log_episode("task", predicted_value=10, actual_value=20, outcome="miss")
    oid = store.start_outing(f"{tag} coffee", 15.0)
    store.set_outing_level(oid, "soft")
    store.start_focus_session(f"{tag} deep work", planned_minutes=50)
    store.upsert_commitment(
        title=f"{tag} standup", start_at="2099-01-01 09:00:00", external_id="shared:1"
    )
    store.add_todo(f"{tag} call dentist", estimate_minutes=10)
    store.set_state("time_estimation_bias", "1.9" if tag == "A" else "1.1")
    store.set_state("pushover_user_key", f"{tag}-key")
    store.dismiss_conflict("shared-signature")
    store.upsert_pattern("time_estimation", "task", observed_value=20, sample_size=3)
    store.record_kind_feedback(f"{tag} meeting", "fyi")
    store.record_mail(account="personal", message_id="shared-msg", subject=f"{tag} hi")
    store.upsert_source(
        kind="imap", account="shared", config=f'{{"host": "{tag}"}}',
        secret_enc=f"{tag}-sealed".encode(),
    )


def test_isolation_reads_only_own_rows(two_users):
    """Each user's reads return only their own rows after both write the full set."""
    a, b, _, _ = two_users
    _write_full_object_set(a, "A")
    _write_full_object_set(b, "B")

    # Episodes / outings / focus / todos / commitments / mail / patterns / kind.
    assert len(a.recent_episodes()) == 1
    assert len(b.recent_episodes()) == 1
    assert [o["intention"] for o in a.active_outings()] == ["A coffee"]
    assert [o["intention"] for o in b.active_outings()] == ["B coffee"]
    assert [s["intended_task"] for s in a.active_focus_sessions()] == ["A deep work"]
    assert [t["title"] for t in a.open_todos()] == ["A call dentist"]
    assert [t["title"] for t in b.open_todos()] == ["B call dentist"]
    assert [c["title"] for c in a.upcoming_commitments()] == ["A standup"]
    assert [m["subject"] for m in a.recent_mail()] == ["A hi"]
    assert a.kind_feedback_examples()[0]["display"] == "A meeting"

    # Coaching state, including delivery routing, is per user.
    assert a.get_state("time_estimation_bias") == "1.9"
    assert b.get_state("time_estimation_bias") == "1.1"
    assert a.get_state("pushover_user_key") == "A-key"
    assert b.get_state("pushover_user_key") == "B-key"

    # Connected sources are per user too: same (kind, account), own row only.
    assert a.get_source("imap", "shared")["config"] == '{"host": "A"}'
    assert b.get_source("imap", "shared")["config"] == '{"host": "B"}'
    assert a.get_source("imap", "shared")["secret_enc"] == b"A-sealed"


def test_isolation_on_conflict_targets_do_not_clobber(two_users):
    """Both users upsert the same keys/ids — two distinct rows, no overwrite."""
    a, b, ua, ub = two_users
    _write_full_object_set(a, "A")
    _write_full_object_set(b, "B")
    conn = a.conn

    # Same external_id in both users' calendars — two commitment rows, one each.
    rows = conn.execute(
        "SELECT user_id FROM commitments WHERE external_id = 'shared:1'"
    ).fetchall()
    assert sorted(r["user_id"] for r in rows) == sorted([ua["id"], ub["id"]])

    # Same coaching key — two rows, distinct values.
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM coaching_state WHERE key = 'time_estimation_bias'"
        ).fetchone()["c"]
        == 2
    )
    # Same dismissed signature — two rows.
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM dismissed_conflicts WHERE signature = 'shared-signature'"
        ).fetchone()["c"]
        == 2
    )
    # Same pattern (type, context_key) — two rows.
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM patterns WHERE pattern_type='time_estimation' "
            "AND context_key='task'"
        ).fetchone()["c"]
        == 2
    )
    # Same (account, message_id) — two rows (the unique is now per user).
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM mail_messages WHERE message_id='shared-msg'"
        ).fetchone()["c"]
        == 2
    )


def test_isolation_no_cross_user_mutation(two_users):
    """A close/cancel by id on the wrong user's row is a no-op (not a leak)."""
    a, b, _, _ = two_users
    oid_a = a.start_outing("A coffee", 15.0)
    # B tries to close A's outing by id — must not succeed or mutate A's row.
    assert b.close_outing(oid_a) is None
    assert a.get_outing(oid_a)["status"] == "active"
    # B cannot even see A's outing by id.
    assert b.get_outing(oid_a) is None


def test_isolation_sources_are_per_user(two_users):
    """One user's connected sources are invisible and immutable to another."""
    a, b, _, _ = two_users
    a.upsert_source(kind="imap", account="personal", config='{"host": "a"}')
    # B sees none of A's sources and can't read A's row by (kind, account).
    assert b.list_sources() == []
    assert b.get_source("imap", "personal") is None
    # B deleting the same (kind, account) is a no-op; A's row survives.
    assert b.delete_source("imap", "personal") is False
    assert a.get_source("imap", "personal") is not None


def test_isolation_decomposition_scoped_through_todo(two_users):
    """A decomposition is reachable only by the user who owns the parent todo."""
    a, b, _, _ = two_users
    tid = a.add_todo("A big task", estimate_minutes=60)
    a.set_decomposition(
        tid, first_step="open the doc", first_step_minutes=2, steps=["draft"], source="heuristic"
    )
    assert a.get_decomposition(tid)["first_step"] == "open the doc"
    # B references A's todo id — sees nothing and cannot edit it.
    assert b.get_decomposition(tid) is None
    assert b.set_step_done(tid, 0) is False
    # A's own progress still works.
    assert a.set_step_done(tid, 0) is True


def test_profile_cache_is_per_user(two_users):
    """Each user has their own single-row profile cache."""
    a, b, _, _ = two_users
    a.set_profile_cache("alice prose", source="llm", model="m", structured="a")
    b.set_profile_cache("bob prose", source="llm", model="m", structured="b")
    assert a.get_profile_cache()["text"] == "alice prose"
    assert b.get_profile_cache()["text"] == "bob prose"


# -- provisioning seeds per-user defaults ------------------------------------


def test_provision_seeds_default_state(unscoped):
    """A freshly provisioned user looks like a fresh single-tenant install."""
    user, _ = provision_user(unscoped, "fresh")
    s = unscoped.scoped(user["id"])
    assert s.get_state("time_estimation_bias") == "1.4"
    assert s.get_state("preferred_briefing_format") == "short"


def test_disable_and_each_user(unscoped):
    """each_user(status='active') excludes a disabled user."""
    provision_user(unscoped, "a")
    provision_user(unscoped, "b")
    unscoped.set_user_status("b", "disabled")
    assert [u["handle"] for u in unscoped.each_user(status="active")] == ["a"]
    assert {u["handle"] for u in unscoped.each_user(status=None)} == {"a", "b"}


# -- learning fan-out --------------------------------------------------------


def test_learn_fanout_updates_each_user_independently(two_users):
    """recompute_patterns on each scoped store derives bias from that user's episodes."""
    a, b, _, _ = two_users
    # A chronically underestimates 2x; B estimates accurately.
    for _ in range(4):
        a.log_episode("task", predicted_value=10, actual_value=20, outcome="miss")
        b.log_episode("task", predicted_value=10, actual_value=10, outcome="success")
    recompute_patterns(a)
    recompute_patterns(b)
    assert a.get_state("time_estimation_bias") == "2.0"
    assert b.get_state("time_estimation_bias") == "1.0"
    # Neither user's patterns leaked into the other's.
    assert a.get_patterns("time_estimation")[0]["observed_value"] == 20.0
    assert b.get_patterns("time_estimation")[0]["observed_value"] == 10.0


def test_coach_all_users_fans_out(tmp_path, capsys):
    """`coach --all-users --dry-run` runs a tick for every active user."""
    from prefrontal.cli import main

    db = str(tmp_path / "p.db")
    assert main(["init-db", "--db-path", db]) == 0
    assert main(["user", "--db-path", db, "add", "alice", "--operator"]) == 0
    assert main(["user", "--db-path", db, "add", "bob"]) == 0
    capsys.readouterr()
    assert main(["coach", "--db-path", db, "--all-users", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "== alice ==" in out and "== bob ==" in out


# -- migration of an existing single-tenant database -------------------------


def _legacy_single_tenant_db() -> sqlite3.Connection:
    """Build an in-memory DB in the *pre*-multi-tenant shape with real data."""
    conn = connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            episode_type TEXT NOT NULL, predicted_value REAL, actual_value REAL,
            acknowledged BOOLEAN, channel TEXT, context TEXT, outcome TEXT, notes TEXT
        );
        CREATE TABLE patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL, context_key TEXT NOT NULL,
            observed_value REAL, predicted_value REAL, variance REAL,
            sample_size INTEGER DEFAULT 0, confidence REAL DEFAULT 0.0,
            last_updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (pattern_type, context_key)
        );
        CREATE TABLE coaching_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE, value TEXT,
            last_updated DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, source TEXT
        );
        CREATE TABLE outings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, intention TEXT NOT NULL,
            time_window_minutes REAL NOT NULL,
            departure_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            home_lat REAL, home_lon REAL, status TEXT NOT NULL DEFAULT 'active',
            last_level TEXT NOT NULL DEFAULT 'none', returned_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, notes TEXT,
            estimate_minutes REAL, priority INTEGER NOT NULL DEFAULT 1,
            deadline DATETIME, energy TEXT, status TEXT NOT NULL DEFAULT 'open',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME
        );
        CREATE TABLE dismissed_conflicts (
            signature TEXT PRIMARY KEY,
            dismissed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE, label TEXT,
            lat REAL NOT NULL, lon REAL NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE kind_feedback (
            title TEXT PRIMARY KEY, display TEXT NOT NULL, kind TEXT NOT NULL,
            llm_kind TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE mail_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL,
            message_id TEXT NOT NULL, thread_id TEXT, sender_name TEXT,
            sender_email TEXT, subject TEXT, received_at DATETIME, snippet TEXT,
            body TEXT, unread BOOLEAN, needs_action BOOLEAN NOT NULL DEFAULT 0,
            urgency TEXT, category TEXT, waiting_on TEXT, summary TEXT,
            triage_source TEXT, policy TEXT NOT NULL DEFAULT 'full', todo_id INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (account, message_id)
        );
        CREATE TABLE profile_cache (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            text TEXT NOT NULL, source TEXT NOT NULL, model TEXT,
            structured TEXT, structured_hash TEXT,
            generated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO coaching_state (key, value, source) VALUES
            ('user_name', 'Tom', 'explicit'),
            ('time_estimation_bias', '1.7', 'inferred');
        INSERT INTO episodes (episode_type, outcome)
            VALUES ('task', 'miss'), ('departure', 'success');
        INSERT INTO outings (intention, time_window_minutes) VALUES ('coffee', 15);
        INSERT INTO todos (title) VALUES ('call dentist'), ('plan trip');
        INSERT INTO patterns (pattern_type, context_key, observed_value)
            VALUES ('drift', 'task', 0.5);
        INSERT INTO dismissed_conflicts (signature) VALUES ('sig-1');
        INSERT INTO places (name, label, lat, lon) VALUES ('gym', 'Gym', 1.0, 2.0);
        INSERT INTO kind_feedback (title, display, kind) VALUES ('standup', 'Standup', 'self');
        INSERT INTO mail_messages (account, message_id, subject)
            VALUES ('personal', 'm-1', 'hello');
        INSERT INTO profile_cache (id, text, source) VALUES (1, 'cached', 'heuristic');
        """
    )
    conn.commit()
    return conn


def test_migration_backfills_legacy_user():
    """Migration creates a legacy user and assigns every row to it; counts survive."""
    conn = _legacy_single_tenant_db()
    before = {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("episodes", "todos", "outings", "patterns", "dismissed_conflicts")
    }
    # The migration is self-contained — it creates users, adds columns, and
    # rebuilds uniqueness without needing schema.sql first.
    result = migrate_to_multi_tenant(conn, handle="tom")
    assert result.migrated is True
    assert result.legacy_handle == "tom"
    assert result.token  # printed once

    legacy_id = conn.execute("SELECT id FROM users WHERE handle='tom'").fetchone()["id"]
    for table, count in before.items():
        # Row counts are unchanged …
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == count
        # … and every row now belongs to the legacy user.
        owned = conn.execute(
            f"SELECT COUNT(*) c FROM {table} WHERE user_id = ?", (legacy_id,)
        ).fetchone()["c"]
        assert owned == count

    # The migrated DB is usable through a scoped store and preserves values.
    store = MemoryStore(conn).scoped(legacy_id)
    assert store.get_state("time_estimation_bias") == "1.7"
    assert len(store.open_todos()) == 2
    assert is_multi_tenant(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == MULTI_TENANT_VERSION


def test_run_migrations_applies_both_ladder_steps():
    """The unified ladder runs the multi-tenant step *and* the column back-fill.

    On a legacy DB, `run_migrations` should (1) upgrade to multi-tenant and
    (2) back-fill columns added later — here `todos.source`, which the legacy
    schema predates — in a single call, before schema.sql is applied.
    """
    conn = _legacy_single_tenant_db()
    assert "source" not in {r["name"] for r in conn.execute("PRAGMA table_info(todos)")}

    result = run_migrations(conn)

    # Step 1: multi-tenant upgrade happened and is reported for token surfacing.
    assert result.migrated is True
    assert result.token
    assert is_multi_tenant(conn)
    # Step 2: the later-added column was back-filled by the same call.
    assert "source" in {r["name"] for r in conn.execute("PRAGMA table_info(todos)")}
    conn.close()


def test_run_migrations_is_noop_on_current_db():
    """run_migrations reports no multi-tenant work on an already-current database."""
    conn = init_db(":memory:")
    try:
        assert run_migrations(conn).migrated is False
    finally:
        conn.close()


def test_migration_rebuilds_all_uniqueness_constraints():
    """Every table whose uniqueness changed is rebuilt to the composite key.

    Regression for a drift where ``_rebuild_constraints`` rebuilt only
    coaching_state/patterns/dismissed_conflicts, leaving places, kind_feedback,
    mail_messages, and profile_cache on their legacy single-column uniques — so
    their ``ON CONFLICT (user_id, …)`` upserts 500'd on a migrated DB.
    """
    conn = _legacy_single_tenant_db()
    result = migrate_to_multi_tenant(conn, handle="tom")
    assert result.migrated is True
    legacy_id = conn.execute("SELECT id FROM users WHERE handle='tom'").fetchone()["id"]

    def table_sql(name: str) -> str:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return " ".join(row["sql"].split())  # normalize whitespace

    # Each rebuilt table now carries the user-scoped composite key from schema.sql.
    assert "UNIQUE (user_id, name)" in table_sql("places")
    assert "PRIMARY KEY (user_id, title)" in table_sql("kind_feedback")
    assert "UNIQUE (user_id, account, message_id)" in table_sql("mail_messages")
    assert "user_id INTEGER PRIMARY KEY" in table_sql("profile_cache")

    # Pre-existing rows survived and were backfilled to the legacy user.
    assert conn.execute(
        "SELECT user_id FROM places WHERE name='gym'"
    ).fetchone()["user_id"] == legacy_id

    # The upserts that used to 500 now resolve in place (no duplicate, value updates).
    store = MemoryStore(conn).scoped(legacy_id)
    store.add_place("gym", 9.0, 9.0, label="Gym2")  # ON CONFLICT (user_id, name)
    gyms = conn.execute("SELECT lat, COUNT(*) c FROM places WHERE name='gym'").fetchone()
    assert gyms["c"] == 1 and gyms["lat"] == 9.0

    store.record_kind_feedback("standup", "fyi")  # ON CONFLICT (user_id, title)
    assert conn.execute(
        "SELECT kind FROM kind_feedback WHERE title='standup'"
    ).fetchone()["kind"] == "fyi"

    # set_profile_cache upserts ON CONFLICT (user_id):
    store.set_profile_cache("fresh", source="llm", model="m", structured="{}")
    cache = conn.execute("SELECT text, COUNT(*) c FROM profile_cache").fetchone()
    assert cache["c"] == 1 and cache["text"] == "fresh"


def test_migration_is_idempotent():
    """Re-running the migration is a no-op (does not duplicate or re-backfill)."""
    conn = _legacy_single_tenant_db()
    first = migrate_to_multi_tenant(conn, handle="tom")
    assert first.migrated is True
    users_before = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    todos_before = conn.execute("SELECT COUNT(*) c FROM todos").fetchone()["c"]

    second = migrate_to_multi_tenant(conn, handle="tom")
    assert second.migrated is False
    assert conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == users_before
    assert conn.execute("SELECT COUNT(*) c FROM todos").fetchone()["c"] == todos_before


def test_init_db_auto_migrates_legacy_file(tmp_path):
    """init_db on a legacy database file upgrades it in place on open."""
    db = tmp_path / "legacy.db"
    # Materialize the legacy shape on disk.
    src = _legacy_single_tenant_db()
    disk = sqlite3.connect(db)
    src.backup(disk)
    disk.close()
    src.close()

    conn = init_db(str(db))
    try:
        assert is_multi_tenant(conn)
        legacy = conn.execute("SELECT id FROM users").fetchone()
        assert legacy is not None
        # All todos were backfilled to the single legacy user.
        owned = conn.execute(
            "SELECT COUNT(*) c FROM todos WHERE user_id = ?", (legacy["id"],)
        ).fetchone()["c"]
        assert owned == 2
    finally:
        conn.close()


def test_seed_user_state_is_non_clobbering(unscoped):
    """Re-seeding preserves a value the user has already changed."""
    user, _ = provision_user(unscoped, "x")
    s = unscoped.scoped(user["id"])
    s.set_state("time_estimation_bias", "2.5", source="explicit")
    seed_user_state(s)  # run again
    assert s.get_state("time_estimation_bias") == "2.5"
