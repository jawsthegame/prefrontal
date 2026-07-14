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

import pytest

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
        "secrets",
        "serve",
        "learn",
        "profile",
        "summarize",
        "briefing",
        "todo",
        "place",
        "fit",
        "mail",
        "calendar",
        "modules",
        "care-recipient",
        "focus",
        "cleanup-drops",
        "cleanup-focus-estimates",
    ):
        # Some commands need a sub-action or a positional; supply a minimal one
        # so parsing succeeds, and assert each top-level command binds a `func`.
        argv = {
            "todo": ["todo", "list"],
            "place": ["place", "list"],
            "mail": ["mail", "list"],
            "fit": ["fit", "30"],
            "secrets": ["secrets", "status"],
            "calendar": ["calendar", "list-sources"],
            "focus": ["focus", "arm"],
            "care-recipient": ["care-recipient", "list"],
        }.get(command, [command])
        args = parser.parse_args(argv)
        assert hasattr(args, "func"), f"{command} did not bind a handler"


def test_proposals_stats_shows_durability_even_when_precision_insufficient(tmp_path, capsys):
    """Durability has its own (lower) sample gate, so `proposals stats` must surface
    it even when there aren't enough resolved proposals to judge precision — a
    regression guard for the early-return that used to hide it."""
    from tests.conftest import scoped_default

    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    # Three accepted state settings: below the 5-proposal precision gate, but at
    # the 3-key durability gate. One is later reverted by an explicit edit.
    with MemoryStore.open(str(db)) as raw:
        store = scoped_default(raw)  # provisions the "tester" operator
        for key, value in (
            ("self_care", "on"),
            ("encouragement", "on"),
            ("responsive_hours_end", "23"),
        ):
            pid = store.add_proposal(kind="state", payload={"key": key, "value": value})
            store.set_proposal_status(pid, "accepted")
            store.set_state(key, value, source="llm_inferred")
        store.set_state("responsive_hours_end", "22", source="explicit")

    assert main(["proposals", "--db-path", str(db), "--user", "tester", "stats"]) == 0
    out = capsys.readouterr().out
    assert "Not enough resolved proposals" in out  # precision below its gate
    assert "Sensor durability: 2/3 accepted settings still standing" in out
    assert "state:responsive_hours_end: reversed since accepting" in out


def test_care_recipient_roster_cli_roundtrip(tmp_path, capsys):
    """`care-recipient set/add/remove/list` wire through and normalize the roster."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tom", "--operator"]) == 0
    capsys.readouterr()

    base = ["care-recipient", "--db-path", str(db), "--user", "tom"]
    # Empty to start.
    assert main([*base, "list"]) == 0
    assert "No care recipients set." in capsys.readouterr().out
    # set replaces; normalization (trim + de-dupe) applies.
    assert main([*base, "set", "  Mom ", "Dad", "mom"]) == 0
    assert "Care recipients: Mom, Dad" in capsys.readouterr().out
    # add appends a new name.
    assert main([*base, "add", "Aunt May"]) == 0
    assert "Care recipients: Mom, Dad, Aunt May" in capsys.readouterr().out
    # remove is case-insensitive.
    assert main([*base, "remove", "dad"]) == 0
    assert "Care recipients: Mom, Aunt May" in capsys.readouterr().out
    # set with no names clears.
    assert main([*base, "set"]) == 0
    assert "No care recipients set." in capsys.readouterr().out


def test_user_resolution_is_case_insensitive(tmp_path, capsys):
    """`--user tom` resolves the `Tom` account — the launchd casing slip that once
    left the coach tick delivering to no one."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "Tom", "--operator"]) == 0
    capsys.readouterr()

    # Wrong case still resolves (learn acts on the resolved user and exits 0).
    assert main(["learn", "--db-path", str(db), "--user", "tom"]) == 0
    out = capsys.readouterr().out
    assert "[Tom]" in out  # acted on the real handle, not a phantom

    # A genuinely unknown handle still fails clearly.
    with pytest.raises(SystemExit, match="No such user"):
        main(["learn", "--db-path", str(db), "--user", "nobody"])


def test_user_email_set_clear_and_add(tmp_path, capsys):
    """`user add --email` and `user email` manage the Google sign-in address."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0

    # Provision with an email up front (normalized to lowercase).
    assert main([
        "user", "--db-path", str(db), "add", "jamie", "--email", "Jamie@Gmail.com",
    ]) == 0
    with MemoryStore.open(str(db)) as store:
        assert store.get_user("jamie")["email"] == "jamie@gmail.com"
        assert store.get_user_by_email("jamie@gmail.com")["handle"] == "jamie"

    # `user list` shows the email.
    capsys.readouterr()
    assert main(["user", "--db-path", str(db), "list"]) == 0
    assert "jamie@gmail.com" in capsys.readouterr().out

    # Change then clear it (omitting the arg clears).
    assert main(["user", "--db-path", str(db), "email", "jamie", "new@x.com"]) == 0
    with MemoryStore.open(str(db)) as store:
        assert store.get_user("jamie")["email"] == "new@x.com"
    assert main(["user", "--db-path", str(db), "email", "jamie"]) == 0
    with MemoryStore.open(str(db)) as store:
        assert store.get_user("jamie")["email"] is None

    # A duplicate email is refused (non-zero exit).
    assert main(["user", "--db-path", str(db), "add", "sam", "--email", "sam@x.com"]) == 0
    assert main(["user", "--db-path", str(db), "email", "jamie", "sam@x.com"]) == 1


def test_user_route_sets_and_clears_per_user_ntfy_topic(tmp_path, capsys):
    """`user route` writes a user's own ntfy topic to their coaching-state, so a
    co-parent's nudges reach *their* phone rather than falling back to the
    operator default (there was no CLI/UI to set this before)."""
    from prefrontal.cli import _resolve_user_store
    from prefrontal.integrations.delivery import resolve_route

    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "jamie"]) == 0
    capsys.readouterr()

    # Set her own topic → command echoes it and it persists as per-user routing.
    assert main([
        "user", "--db-path", str(db), "route", "jamie",
        "--ntfy-topic", "prefrontal-jamie-abc123",
    ]) == 0
    out = capsys.readouterr().out
    assert "prefrontal-jamie-abc123" in out and "Updated: ntfy_topic" in out
    with MemoryStore.open(str(db)) as store:
        route = resolve_route(_resolve_user_store(store, "jamie"))
    assert route.ntfy_topic == "prefrontal-jamie-abc123"

    # Empty string clears it (no per-user topic + no operator default → empty).
    assert main(["user", "--db-path", str(db), "route", "jamie", "--ntfy-topic", ""]) == 0
    with MemoryStore.open(str(db)) as store:
        assert resolve_route(_resolve_user_store(store, "jamie")).ntfy_topic == ""

    # A secret (ntfy token) is never echoed back in the clear.
    capsys.readouterr()
    assert main([
        "user", "--db-path", str(db), "route", "jamie", "--ntfy-token", "tk_supersecret",
    ]) == 0
    assert "tk_supersecret" not in capsys.readouterr().out

    # Unknown user fails clearly.
    with pytest.raises(SystemExit, match="No such user"):
        main(["user", "--db-path", str(db), "route", "nobody"])


def test_user_connect_link_builds_deep_link_with_route(tmp_path, capsys):
    """`user connect-link` emits a prefrontal://connect URL carrying the base URL
    and handle, so a new phone can onboard by scanning a QR. Native APNs push is
    the product path (the app registers its own device token on launch), so the
    link carries NO ntfy hints even when a dev-shim topic happens to be set."""
    from urllib.parse import parse_qs, urlsplit

    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "sam", "--display-name", "Sam"]) == 0
    assert main([
        "user", "--db-path", str(db), "route", "sam", "--ntfy-topic", "prefrontal-sam-9f2q",
    ]) == 0
    capsys.readouterr()

    # No token by default (it's shown once at provisioning, not re-readable):
    # the link still carries URL + handle, and flags the omission.
    assert main([
        "user", "--db-path", str(db), "connect-link", "sam",
        "--base-url", "https://agent-1.tail8b0a.ts.net/",
    ]) == 0
    out = capsys.readouterr().out
    link = next(w for w in out.split() if w.startswith("prefrontal://connect?"))
    parts = urlsplit(link)
    assert parts.scheme == "prefrontal" and parts.netloc == "connect"
    q = parse_qs(parts.query)
    assert q["url"] == ["https://agent-1.tail8b0a.ts.net"]  # trailing slash trimmed
    assert "ntfy_topic" not in q                            # native push — no ntfy in the QR
    assert q["handle"] == ["sam"] and q["name"] == ["Sam"]
    assert "token" not in q
    assert "no token embedded" in out


def test_user_connect_link_rotate_embeds_token_and_needs_base_url(tmp_path, capsys):
    """`--rotate` mints and embeds a fresh token; with no OAUTH_BASE_URL and no
    --base-url the command refuses rather than emitting a useless link."""
    from urllib.parse import parse_qs, urlsplit

    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "sam"]) == 0
    capsys.readouterr()

    assert main([
        "user", "--db-path", str(db), "connect-link", "sam",
        "--base-url", "https://x.ts.net", "--rotate",
    ]) == 0
    out = capsys.readouterr().out
    link = next(w for w in out.split() if w.startswith("prefrontal://connect?"))
    token = parse_qs(urlsplit(link).query)["token"][0]
    # The embedded token is the live one: its hash resolves the user row.
    from prefrontal.memory._helpers import sha256_hex

    with MemoryStore.open(str(db)) as store:
        row = store.get_user_by_token_hash(sha256_hex(token))
        assert row is not None and row["handle"] == "sam"

    # An unknown user fails clearly.
    assert main([
        "user", "--db-path", str(db), "connect-link", "nobody", "--base-url", "https://x.ts.net",
    ]) == 1


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


def test_notify_delivers_through_configured_route(tmp_path, capsys, monkeypatch):
    """`notify` publishes a test push via the real delivery client to the user's
    route (here a per-user ntfy topic pointed at a local receiver). Native APNs is
    the product path; the dev shim (PREFRONTAL_NTFY_DEV) makes ntfy observable."""
    from prefrontal.config import get_settings

    monkeypatch.setenv("PREFRONTAL_NTFY_DEV", "1")
    get_settings.cache_clear()
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
    get_settings.cache_clear()


def test_briefing_deliver_publishes_through_route(tmp_path, capsys, monkeypatch):
    """`briefing --deliver` publishes the digest as a push to the user's own route —
    the native twin of the morning-briefing n8n workflow. The dev shim
    (PREFRONTAL_NTFY_DEV) makes the ntfy transport observable in-test."""
    from prefrontal.config import get_settings

    monkeypatch.setenv("PREFRONTAL_NTFY_DEV", "1")
    get_settings.cache_clear()
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    capsys.readouterr()

    srv, url, received = _local_ntfy_server()
    try:
        with MemoryStore.open(str(db)) as raw:
            uid = next(u["id"] for u in raw.list_users() if u["handle"] == "tester")
            scoped = raw.scoped(uid)
            scoped.set_state("ntfy_server", url, source="explicit")
            scoped.set_state("ntfy_topic", "briefing-topic", source="explicit")

        rc = main(["briefing", "--db-path", str(db), "--user", "tester", "--deliver"])
        assert rc == 0
    finally:
        srv.shutdown()

    out = capsys.readouterr().out
    assert "sent" in out
    assert received and received[0]["topic"] == "briefing-topic"
    assert received[0]["priority"] == 3  # push → normal priority
    assert received[0]["message"]  # the rendered briefing text rode along
    get_settings.cache_clear()


def test_briefing_deliver_exit_1_without_route(tmp_path, capsys):
    """`briefing --deliver` exits non-zero when the user has no transport configured."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    capsys.readouterr()
    # No ntfy/Pushover route set → nothing can be delivered → exit 1.
    assert main(["briefing", "--db-path", str(db), "--user", "tester", "--deliver"]) == 1


def test_focus_arm_cli_arms_live_block(tmp_path, capsys, monkeypatch):
    """`focus arm` auto-starts a session from a live calendar focus block — the
    native twin of POST /webhooks/focus/arm (no n8n poll)."""
    from datetime import timedelta

    from prefrontal.cli import _resolve_user_store
    from prefrontal.clock import TS_FMT
    from prefrontal.impact import utcnow

    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    capsys.readouterr()

    # Pin the arm path's clock to a fixed midday. `focus arm` (arm_focus_session)
    # reads prefrontal.impact.utcnow and scopes candidate blocks to *today's local
    # day*; anchored to the real clock, a block created in the minutes around local
    # midnight has its start roll into yesterday and drop out of today's window — a
    # spurious flake unrelated to the behavior under test (issue #604).
    now = utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr("prefrontal.impact.utcnow", lambda: now)
    with MemoryStore.open(str(db)) as raw:
        scoped = _resolve_user_store(raw, "tester")
        scoped.upsert_commitment(
            title="Deep work: the RFC",
            start_at=(now - timedelta(minutes=20)).strftime(TS_FMT),
            end_at=(now + timedelta(minutes=40)).strftime(TS_FMT),
            hardness="soft",
        )

    assert main(["focus", "arm", "--db-path", str(db), "--user", "tester"]) == 0
    out = capsys.readouterr().out
    assert "Armed focus: “the RFC”" in out

    with MemoryStore.open(str(db)) as raw:
        scoped = _resolve_user_store(raw, "tester")
        active = scoped.active_focus_sessions()
        assert active and active[0]["intended_task"] == "the RFC"

    # Idempotent: a second arm doesn't stack a session.
    assert main(["focus", "arm", "--db-path", str(db), "--user", "tester"]) == 0
    assert "No arm:" in capsys.readouterr().out


def test_deliver_panic_publishes_when_overwhelmed(tmp_path, capsys):
    """The native panic delivery (used by `coach --deliver`) publishes an overwhelm
    nudge through the user's route — so panic no longer needs the n8n poll."""
    from datetime import timedelta

    from prefrontal.cli import _deliver_panic
    from prefrontal.config import Settings
    from prefrontal.impact import utcnow

    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tom", "--operator"]) == 0
    capsys.readouterr()

    srv, url, received = _local_ntfy_server()
    try:
        with MemoryStore.open(str(db)) as raw:
            uid = next(u["id"] for u in raw.list_users() if u["handle"] == "tom")
            scoped = raw.scoped(uid)
            scoped.set_state("ntfy_server", url, source="explicit")
            scoped.set_state("ntfy_topic", "tom-alerts", source="explicit")
            scoped.set_state("responsive_hours_start", "0")  # always responsive (no defer)
            scoped.set_state("responsive_hours_end", "0")
            now = utcnow()
            for t in ("A", "B", "C"):  # three overdue todos → overwhelmed
                scoped.add_todo(t, deadline=(now - timedelta(days=1)).strftime("%Y-%m-%d"))
            # ntfy_dev=True: exercise delivery via the mock ntfy receiver (native
            # APNs needs real creds); the test asserts routing, not the transport.
            _deliver_panic(raw, scoped, Settings(ntfy_dev=True), now)
    finally:
        srv.shutdown()

    assert received, "panic nudge should have been published"
    assert received[0]["topic"] == "tom-alerts"
    assert received[0]["priority"] == 4  # sound → high priority, never local TTS


def test_cleanup_drops_dry_run_then_apply(tmp_path, capsys):
    """`cleanup-drops` reports past hygiene todo-drop misses, and only rewrites
    them to `discarded` when `--apply` is passed (dry-run by default)."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    capsys.readouterr()

    with MemoryStore.open(str(db)) as raw:
        uid = next(u["id"] for u in raw.list_users() if u["handle"] == "tester")
        scoped = raw.scoped(uid)
        eid = scoped.log_episode(
            "task",
            context="todo dropped: mis-captured",
            outcome="miss",
            notes="dropped after 0.5d open",
        )

    # Dry run: reports one, writes nothing.
    assert main(["cleanup-drops", "--db-path", str(db), "--user", "tester"]) == 0
    out = capsys.readouterr().out
    assert "would reclassify 1" in out
    with MemoryStore.open(str(db)) as raw:
        assert raw.scoped(uid).get_episode(eid)["outcome"] == "miss"

    # Apply: rewrites it.
    assert main(
        ["cleanup-drops", "--db-path", str(db), "--user", "tester", "--apply"]
    ) == 0
    out = capsys.readouterr().out
    assert "reclassified 1" in out
    with MemoryStore.open(str(db)) as raw:
        assert raw.scoped(uid).get_episode(eid)["outcome"] == "discarded"


def test_cleanup_focus_estimates_dry_run_then_apply(tmp_path, capsys):
    """`cleanup-focus-estimates` reports switched focus blocks feeding the estimate
    bias, and only nulls their actual_value when `--apply` is passed."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    capsys.readouterr()

    with MemoryStore.open(str(db)) as raw:
        uid = next(u["id"] for u in raw.list_users() if u["handle"] == "tester")
        scoped = raw.scoped(uid)
        eid = scoped.log_episode(
            "task", predicted_value=60.0, actual_value=8.0,
            context="focus switched: deep work", outcome="partial",
        )

    assert main(["cleanup-focus-estimates", "--db-path", str(db), "--user", "tester"]) == 0
    out = capsys.readouterr().out
    assert "would clear 1" in out
    with MemoryStore.open(str(db)) as raw:
        assert raw.scoped(uid).get_episode(eid)["actual_value"] == 8.0

    assert main(
        ["cleanup-focus-estimates", "--db-path", str(db), "--user", "tester", "--apply"]
    ) == 0
    out = capsys.readouterr().out
    assert "cleared 1" in out
    with MemoryStore.open(str(db)) as raw:
        assert raw.scoped(uid).get_episode(eid)["actual_value"] is None


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


def test_clarify_check_list_resolve_and_guide(tmp_path, capsys):
    """The `clarify` command wires through: check → list → resolve → guide.

    Exercises the argument wiring for the whole group (the coaching tick uses the
    same sweep). The default Ollama is unreachable in tests, so detection takes
    the heuristic path — which still recognizes "Tax".
    """
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    assert main(["todo", "--db-path", str(db), "add", "Tax", "--priority", "2"]) == 0
    capsys.readouterr()

    # check: flags the ambiguous "Tax" todo.
    assert main(["clarify", "--db-path", str(db), "check"]) == 0
    out = capsys.readouterr().out
    assert "Tax" in out and "has guide" in out

    # list: the pending question is there; grab its id.
    assert main(["clarify", "--db-path", str(db), "list"]) == 0
    listed = capsys.readouterr().out
    cid = int(listed.split("#", 1)[1].split(" ", 1)[0])

    # resolve by option 0 → recognized task type, so the playbook prints.
    assert main(["clarify", "--db-path", str(db), "resolve", str(cid), "--option", "0"]) == 0
    resolved = capsys.readouterr().out
    assert "Resolved" in resolved and "Filing your tax return" in resolved

    # A re-check asks nothing new (the item now has history).
    assert main(["clarify", "--db-path", str(db), "check"]) == 0
    assert "No new ambiguous items" in capsys.readouterr().out

    # guide: preview a playbook by type (no store needed); unknown type exits 1.
    assert main(["clarify", "--db-path", str(db), "guide", "tax_filing"]) == 0
    assert "Filing your tax return" in capsys.readouterr().out
    assert main(["clarify", "--db-path", str(db), "guide", "nope"]) == 1
    assert "No playbook" in capsys.readouterr().err


def test_clarify_dismiss_and_bad_ids(tmp_path, capsys):
    """`clarify dismiss` marks an item not-ambiguous; bad ids exit non-zero."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    assert main(["todo", "--db-path", str(db), "add", "Mom"]) == 0
    assert main(["clarify", "--db-path", str(db), "check"]) == 0
    listed_id = int(capsys.readouterr().out.split("#", 1)[1].split(" ", 1)[0])

    assert main(["clarify", "--db-path", str(db), "dismiss", str(listed_id)]) == 0
    assert "Dismissed" in capsys.readouterr().out
    # Dismissing again (no longer pending) and an unknown id both exit 1.
    assert main(["clarify", "--db-path", str(db), "dismiss", str(listed_id)]) == 1
    assert main(["clarify", "--db-path", str(db), "resolve", "999999", "--option", "0"]) == 1
    capsys.readouterr()


def test_clarify_localize_toggle_and_guide(tmp_path, capsys):
    """`clarify localize on --zip` opts in and localizes a guide; `off` reverts."""
    db = tmp_path / "prefrontal.db"
    assert main(["init-db", "--db-path", str(db)]) == 0
    assert main(["user", "--db-path", str(db), "add", "tester", "--operator"]) == 0
    capsys.readouterr()

    # Seeded off → guide uses the generic phrasing.
    assert main(["clarify", "--db-path", str(db), "guide", "license_renewal"]) == 0
    assert "your area" in capsys.readouterr().out

    # Opt in with an explicit ZIP → guide weaves it in.
    assert main(["clarify", "--db-path", str(db), "localize", "on", "--zip", "19027"]) == 0
    assert "19027" in capsys.readouterr().out
    assert main(["clarify", "--db-path", str(db), "guide", "license_renewal"]) == 0
    guide = capsys.readouterr().out
    assert "19027" in guide and "your area" not in guide

    # Opt back out → generic again.
    assert main(["clarify", "--db-path", str(db), "localize", "off"]) == 0
    capsys.readouterr()
    assert main(["clarify", "--db-path", str(db), "guide", "license_renewal"]) == 0
    assert "your area" in capsys.readouterr().out
