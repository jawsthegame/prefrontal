"""Smoke tests for the ``prefrontal`` CLI entry point.

The command logic (pattern recompute, profile build, etc.) is covered in depth
elsewhere; these tests exercise the argument wiring in ``build_parser`` /
``main`` — the seam the nightly ``deploy/learn.sh`` actually invokes — so a
broken subcommand registration or exit-code contract is caught.
"""

from __future__ import annotations

from prefrontal.cli import build_parser, main


def test_init_db_then_learn_roundtrip(tmp_path, capsys):
    """`init-db` then `learn` against a fresh DB both wire through and exit 0.

    This is the exact pair the scheduled learning pass runs, so it guards the
    end-to-end CLI path, not just the underlying functions.
    """
    db = tmp_path / "prefrontal.db"

    assert main(["init-db", "--db-path", str(db)]) == 0
    assert db.exists()

    # `learn` on a DB with no episodes is a valid no-op, not an error.
    assert main(["learn", "--db-path", str(db)]) == 0

    out = capsys.readouterr().out
    assert "Recomputed patterns from 0 episodes." in out


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
        "fit",
        "mail",
        "modules",
    ):
        # Some commands need a sub-action or a positional; supply a minimal one
        # so parsing succeeds, and assert each top-level command binds a `func`.
        argv = {
            "todo": ["todo", "list"],
            "mail": ["mail", "list"],
            "fit": ["fit", "30"],
        }.get(command, [command])
        args = parser.parse_args(argv)
        assert hasattr(args, "func"), f"{command} did not bind a handler"
