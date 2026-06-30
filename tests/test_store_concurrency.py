"""Concurrency tests for :class:`MemoryStore`'s connection modes.

These guard the fix for the dashboard's commitments widget randomly flipping
between one item, all items, and empty. The cause was a single shared
``sqlite3.Connection`` used from FastAPI's threadpool: the dashboard fetches
six endpoints at once, so concurrent reads interleaved on the one connection
and returned truncated, empty, or full result sets at random. The store now
opens one connection per thread (:meth:`MemoryStore.threaded`).
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from prefrontal.memory.store import MemoryStore


def _start_at(minutes_from_now: float) -> str:
    """A UTC ``YYYY-MM-DD HH:MM:SS`` start timestamp in the future."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def test_construction_requires_exactly_one_source():
    """Neither or both of conn / connection_factory is a programming error."""
    with pytest.raises(ValueError):
        MemoryStore()
    with pytest.raises(ValueError):
        MemoryStore(conn=object(), connection_factory=lambda: object())  # type: ignore[arg-type]


def test_threaded_rejects_memory_database():
    """Per-thread mode cannot share a private ':memory:' database."""
    with pytest.raises(ValueError):
        MemoryStore.threaded(":memory:")


def test_threaded_hands_each_thread_its_own_connection(tmp_path):
    """A thread reuses its connection; different threads get different ones."""
    store = MemoryStore.threaded(str(tmp_path / "memory.db"))
    try:
        # Same thread → same object, twice.
        assert store.conn is store.conn

        seen: list[int] = []
        barrier = threading.Barrier(4)

        def grab() -> int:
            barrier.wait()  # ensure the threads overlap, not run serially
            return id(store.conn)

        with ThreadPoolExecutor(max_workers=4) as pool:
            seen = list(pool.map(lambda _: grab(), range(4)))

        # Four distinct connection objects, one per worker thread.
        assert len(set(seen)) == 4
    finally:
        store.close()


def test_concurrent_reads_always_see_every_commitment(tmp_path):
    """Hammering the read path from many threads never truncates the result.

    On the old shared-connection code this returned the wrong count (or raised)
    intermittently — exactly the widget's one/all/empty flip. With a connection
    per thread, every concurrent read sees the full, stable set.
    """
    store = MemoryStore.threaded(str(tmp_path / "memory.db"))
    try:
        expected = 12
        for i in range(expected):
            store.upsert_commitment(
                title=f"Event {i}",
                start_at=_start_at(60 + i),
                external_id=f"feed:{i}",
            )

        def read_count(_: int) -> int:
            return len(store.upcoming_commitments())

        with ThreadPoolExecutor(max_workers=16) as pool:
            counts = list(pool.map(read_count, range(400)))

        # Every single concurrent read must see all 12 — no flicker.
        assert set(counts) == {expected}
    finally:
        store.close()


def test_concurrent_reads_and_writes_stay_consistent(tmp_path):
    """Interleaved writes and reads never crash or return a partial row count.

    Readers may observe the count before or after a given insert, but each row
    count must be one the database actually held — never a torn read.
    """
    store = MemoryStore.threaded(str(tmp_path / "memory.db"))
    try:
        base = 8
        for i in range(base):
            store.upsert_commitment(
                title=f"Base {i}", start_at=_start_at(30 + i), external_id=f"base:{i}"
            )

        # Keep the row total under upcoming_commitments()'s default LIMIT so a
        # count reflects every row, not a truncation by the query itself.
        n_tasks = 60
        writer_ns = [n for n in range(n_tasks) if n % 3 == 0]

        def writer(i: int) -> None:
            store.upsert_commitment(
                title=f"New {i}", start_at=_start_at(500 + i), external_id=f"new:{i}"
            )

        def reader(_: int) -> int:
            return len(store.upcoming_commitments())

        with ThreadPoolExecutor(max_workers=12) as pool:
            tasks = [
                pool.submit(writer if n % 3 == 0 else reader, n) for n in range(n_tasks)
            ]
            results = [t.result() for t in tasks]
        counts = [c for c in results if c is not None]

        # Reads land somewhere between the starting count and the final total;
        # none are torn (negative, zero when rows exist, or above the ceiling).
        final = len(store.upcoming_commitments())
        assert final == base + len(writer_ns)
        assert all(base <= c <= final for c in counts)
    finally:
        store.close()


def test_close_releases_every_thread_connection(tmp_path):
    """close() shuts every per-thread connection; reuse after close fails."""
    store = MemoryStore.threaded(str(tmp_path / "memory.db"))

    conns: list[object] = []

    def touch(i: int) -> None:
        store.upsert_commitment(
            title="x", start_at=_start_at(10), external_id=f"touch:{i}"
        )
        conns.append(store.conn)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(touch, range(3)))

    assert len({id(c) for c in conns}) == 3  # one connection per worker thread

    store.close()

    # Every connection the factory opened is now closed.
    for c in conns:
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")  # type: ignore[attr-defined]
