"""Shared CLI helpers used across command groups.

Small building blocks the ``prefrontal`` subcommands lean on — resolving the
acting user's scoped store, expanding ``--all-users`` fan-out, and the
connect-link / QR helpers for onboarding. Kept here (not in ``cli/__init__``) so
each command-group module can import them without importing the top-level parser.
"""

from __future__ import annotations

import argparse

from prefrontal.memory.store import MemoryStore


def _resolve_user_store(store: MemoryStore, handle: str | None) -> MemoryStore:
    """Return ``store`` scoped to a user chosen by ``handle`` (or the only one).

    Handle matching is case-insensitive (an exact-case match still wins), so a
    launchd/cron ``--user tom`` resolves the ``Tom`` account instead of silently
    dropping the tick — the casing slip that once left the coach agent delivering
    to no one.

    Args:
        store: An unscoped store.
        handle: The user's handle, or ``None`` to auto-pick when exactly one
            user exists.

    Returns:
        A store scoped to the resolved user.

    Raises:
        SystemExit: With a clear message if the handle is unknown (or matches
            more than one account only by case), or if no handle was given and
            zero/many users exist.
    """
    users = store.list_users()
    if handle is not None:
        match = next((u for u in users if u["handle"] == handle), None)
        if match is None:
            # Fall back to a case-insensitive match (handles are UNIQUE but
            # case-sensitively, so guard against two case-variant accounts).
            ci = [u for u in users if u["handle"].lower() == handle.lower()]
            if len(ci) > 1:
                names = ", ".join(u["handle"] for u in ci)
                raise SystemExit(
                    f"Ambiguous user '{handle}' — matches {names} by case; "
                    "pass the exact handle."
                )
            match = ci[0] if ci else None
        if match is None:
            raise SystemExit(f"No such user '{handle}'. Run `prefrontal user list`.")
        return store.scoped(match["id"])
    if not users:
        raise SystemExit(
            "No users provisioned. Run `prefrontal user add <handle>` first."
        )
    if len(users) > 1:
        handles = ", ".join(u["handle"] for u in users)
        raise SystemExit(
            f"Multiple users exist ({handles}); pass --user <handle>."
        )
    return store.scoped(users[0]["id"])


def _user_targets(
    store: MemoryStore, args: argparse.Namespace
) -> list[tuple[str, MemoryStore]]:
    """Resolve the ``(handle, scoped_store)`` pairs a data command should act on.

    With ``--all-users`` (for commands that define it) this is every active user;
    otherwise the single user named by ``--user`` (or the sole user). The handle
    is only a label for output. ``store`` must be unscoped.
    """
    if getattr(args, "all_users", False):
        return [(u["handle"], store.scoped(u["id"])) for u in store.each_user()]
    scoped = _resolve_user_store(store, args.user)
    handle = next(
        (u["handle"] for u in store.list_users() if u["id"] == scoped.user_id),
        str(scoped.user_id),
    )
    return [(handle, scoped)]


def build_connect_link(
    base_url: str,
    *,
    token: str | None = None,
    ntfy_server: str | None = None,
    ntfy_topic: str | None = None,
    handle: str | None = None,
    display_name: str | None = None,
) -> str:
    """Build the ``prefrontal://connect?…`` deep link the iOS app consumes.

    This is the operator→phone handoff: rendered as a QR on the setup sheet, it
    lets a new phone connect by pointing its camera (iOS Camera recognises the
    custom scheme) rather than hand-typing a base URL and a long token. The iOS
    parser (``ios/Prefrontal/Onboarding/ConnectPayload.swift``) reads the same
    query keys; keep the two in sync.

    Only ``base_url`` is required. ``token`` is omitted when unknown (the user
    pastes it), and the ntfy hints just prefill the notifications step.

    Args:
        base_url: The deployment origin, e.g. ``https://agent-1.tail….ts.net``.
        token: The user's ``X-Prefrontal-Token`` (embedded only when known).
        ntfy_server: ntfy server the topic lives on.
        ntfy_topic: The user's own ntfy topic.
        handle: The user's handle (advisory).
        display_name: Name shown on the app's "you're all set" screen.

    Returns:
        A ``prefrontal://connect?…`` URL with percent-encoded query values.
    """
    from urllib.parse import urlencode

    params: list[tuple[str, str]] = [("url", base_url.rstrip("/"))]
    if token:
        params.append(("token", token))
    if ntfy_server:
        params.append(("ntfy_server", ntfy_server))
    if ntfy_topic:
        params.append(("ntfy_topic", ntfy_topic))
    if handle:
        params.append(("handle", handle))
    if display_name:
        params.append(("name", display_name))
    return "prefrontal://connect?" + urlencode(params)


def _print_qr(data: str) -> None:
    """Render ``data`` as a terminal QR via the optional ``segno`` extra.

    QR rendering is opt-in (``pip install 'prefrontal[qr]'``) so the base install
    stays dependency-light; without it we point at the extra and leave the plain
    link (which is always printed) as the fallback.
    """
    try:
        import segno
    except ModuleNotFoundError:
        print(
            "  (install the QR extra to render a code here: "
            "pip install 'prefrontal[qr]' — or paste the link into any QR maker.)"
        )
        return
    print()
    segno.make(data, error="m").terminal(compact=True)


