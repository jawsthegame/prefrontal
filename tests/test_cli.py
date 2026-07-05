"""Smoke tests for the ``prefrontal`` CLI entry point.

The command logic (pattern recompute, profile build, etc.) is covered in depth
elsewhere; these tests exercise the argument wiring in ``build_parser`` /
``main`` — the seam the nightly ``deploy/learn.sh`` actually invokes — so a
broken subcommand registration or exit-code contract is caught.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from prefrontal.cli import build_parser, main
from prefrontal.memory.store import MemoryStore


def test_init_db_then_learn_roundtrip(tmp_path, capsys):
    """`init-db` then `learn` against a fresh DB both wire through and exit 0.

    This is the exact pair the scheduled learning pass runs, so it guards the
    end-to-end CLI path, not just the underlying functions.
    """
    db = tmp_path / "prefrontal.db"

    assert main(["init-db", "--db-path", str(db)]) == 0
    assert db.exists()

    # Multi-tenant: `learn` acts on a user, so provision one first (the nightly
    # pass runs against provisioned users).
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0

    # `learn` on a DB with no episodes is a valid no-op, not an error.
    assert main(["learn", "--db-path", str(db)]) == 0

    out = capsys.readouterr().out
    # Multi-tenant `learn` prefixes each line with the user it acted on.
    assert "recomputed patterns from 0 episodes." in out


def test_build_parser_registers_expected_commands():
    """Every documented subcommand parses and binds a handler (no silent drop)."""
    parser = build_parser()
    for command in (
        "init-db",
        "serve",
        "learn",
        "profile",
        "summarize",
        "briefing",
        "todo",
        "place",
        "fit",
        "mail",
        "modules",
    ):
        # Some commands need a sub-action or a positional; supply a minimal one
        # so parsing succeeds, and assert each top-level command binds a `func`.
        argv = {
            "todo": ["todo", "list"],
            "place": ["place", "list"],
            "mail": ["mail", "list"],
            "fit": ["fit", "30"],
        }.get(command, [command])
        args = parser.parse_args(argv)
        assert hasattr(args, "func"), f"{command} did not bind a handler"


def test_place_add_then_list_roundtrip(tmp_path, capsys):
    """`place add` normalizes + stores an alias; `place list` prints it."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    capsys.readouterr()  # drop setup output

    assert main(
        ["place", "--db-path", str(db), "add", "Dentist Office", "37.77", "-122.41"]
    ) == 0
    assert main(["place", "--db-path", str(db), "list"]) == 0
    out = capsys.readouterr().out
    assert "dentist office" in out  # normalized match key
    assert "37.77" in out and "-122.41" in out


def _local_ntfy_server():
    """Start a throwaway ntfy-compatible receiver; return (server, url, received)."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"id":"local"}')

        def log_message(self, *a):  # keep test output clean
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", received


def test_notify_delivers_through_configured_route(tmp_path, capsys):
    """`notify` publishes a test push via the real delivery client to the user's
    route (here a per-user ntfy topic pointed at a local receiver)."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    capsys.readouterr()

    srv, url, received = _local_ntfy_server()
    try:
        # Point this user's route at the local receiver (state wins over settings).
        with MemoryStore.open(str(db)) as raw:
            uid = next(u["id"] for u in raw.list_users() if u["handle"] == "tester")
            scoped = raw.scoped(uid)
            scoped.set_state("ntfy_server", url, source="explicit")
            scoped.set_state("ntfy_topic", "test-topic", source="explicit")

        rc = main(
            ["notify", "--db-path", str(db), "--user", "tester", "-m", "hello mini",
             "--channel", "sound"]
        )
        assert rc == 0
    finally:
        srv.shutdown()

    out = capsys.readouterr().out
    assert "delivered=True" in out
    assert received and received[0]["message"] == "hello mini"
    assert received[0]["topic"] == "test-topic"
    assert received[0]["priority"] == 4  # sound → priority 4


def test_notify_reports_when_no_transport_configured(tmp_path, capsys, monkeypatch):
    """With no ntfy/Pushover configured, `notify` exits non-zero and says so."""
    # Neutralize any operator defaults leaking from the environment.
    for var in ("NTFY_TOPIC", "NTFY_SERVER", "NTFY_TOKEN",
                "PUSHOVER_TOKEN", "PUSHOVER_USER_KEY"):
        monkeypatch.delenv(var, raising=False)
    from prefrontal.config import get_settings

    get_settings.cache_clear()

    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    capsys.readouterr()

    rc = main(["notify", "--db-path", str(db), "--user", "tester"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no transport" in err.lower()
    get_settings.cache_clear()
