"""Command line entry point for Prefrontal.

Exposes subcommands, wired up as the ``prefrontal`` console script in
``pyproject.toml``:

- ``prefrontal init-db`` — create the SQLite memory database.
- ``prefrontal user`` — provision users (add/list/rotate/disable/route/connect-link).
- ``prefrontal migrate-multi-tenant`` — upgrade a single-tenant DB in place.
- ``prefrontal serve`` — run the webhook listener with uvicorn.
- ``prefrontal learn`` — recompute derived patterns from accumulated episodes.
- ``prefrontal profile`` — print (or write) the structured behavioral profile;
  ``--todo <id>`` prints the queryable behavioral model for one todo instead.
- ``prefrontal summarize`` — LLM-summarize the profile (Ollama); cache it for
  ``GET /profile`` and write ``profile-<handle>.md``.
- ``prefrontal briefing`` — print today's morning digest (``--llm`` for prose).
- ``prefrontal coach`` — run one coaching tick: what's due + on which channel.
- ``prefrontal encourage`` — print today's recovery message if the day's rough.
- ``prefrontal panic`` — triage what's on fire right now + one first step.
- ``prefrontal next`` — the single honest next thing to do right now.
- ``prefrontal todo`` — add/list/done open todos (open loops).
- ``prefrontal fit`` — show open todos that fit N minutes of free time.
- ``prefrontal mail`` — ingest/fetch/list triaged email (list/sync/fetch).

Multi-tenant: the data commands (``learn``, ``summarize``, ``profile``,
``briefing``, ``panic``, ``next``, ``todo``, ``fit``, ``mail``) act on one user, chosen with
``--user <handle>``; ``learn``/``summarize`` also take ``--all-users`` to fan
out (the nightly default). A command errors if no user is given and more than
one exists.

Run ``prefrontal --help`` or ``prefrontal <command> --help`` for details.
"""

from __future__ import annotations

import argparse

from prefrontal import __version__
from prefrontal.cli import (
    admin,
    calendar_care,
    capture,
    catalog,
    coaching_cmds,
    insights,
    mail_cmds,
    people_cmds,
    todos_cmds,
)
from prefrontal.cli._common import _resolve_user_store
from prefrontal.config import get_settings
from prefrontal.log import configure_logging
from prefrontal.memory.store import MemoryStore


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser` with subcommands attached.
    """
    parser = argparse.ArgumentParser(
        prog="prefrontal",
        description="Prefrontal — an open source executive function agent system.",
    )
    parser.add_argument("--version", action="version", version=f"prefrontal {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    admin.register(sub)

    insights.register(sub)

    coaching_cmds.register(sub)

    capture.register(sub)
    people_cmds.register(sub)
    todos_cmds.register(sub)

    mail_cmds.register(sub)

    calendar_care.register(sub)

    catalog.register(sub)

    return parser


#: CLI subcommand → the feature invoking it records, for the usage loop's
#: ``invoked`` half. Only the *pull* commands the operator runs to get value
#: (not the engine ticks / provisioning / write commands), and only single-user
#: runs (an ``--all-users`` cron fan-out isn't "me using a feature").
_CLI_PULL_FEATURES = {
    "panic": "panic",
    "next": "scheduling",
    "day": "scheduling",
    "briefing": "briefing",
    "balance": "balance",
    "encourage": "encouragement",
    "open-day": "encouragement",
    "fit": "scheduling",
    "profile": "profile",
    "modules": "modules",
    "clarify": "clarify",
}


def _record_cli_invocation(args: argparse.Namespace) -> None:
    """Best-effort ``invoked`` stamp for a pull command (never raises).

    Reproduces the same db-path + user resolution the commands use, so the event
    is scoped to exactly the user the command acted on. A telemetry failure must
    never turn a successful command into an error, so everything is swallowed.
    """
    feature = _CLI_PULL_FEATURES.get(getattr(args, "command", None) or "")
    if feature is None or getattr(args, "all_users", False):
        return
    try:
        settings = get_settings()
        db_path = getattr(args, "db_path", None) or settings.db_path
        with MemoryStore.open(db_path) as unscoped:
            store = _resolve_user_store(unscoped, getattr(args, "user", None))
            store.record_feature_event(feature, "invoked", source="cli")
    except (Exception, SystemExit):  # noqa: BLE001 — telemetry is best-effort
        pass


def main(argv: list[str] | None = None) -> int:
    """Program entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    if rc == 0:
        _record_cli_invocation(args)
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
