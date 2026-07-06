"""Service layer over the per-user source registry.

Owns the encryption boundary: callers hand plaintext secrets *in* and get
plaintext *out* here, while the sealed bytes never leave this module and the
:class:`~prefrontal.memory.repos.sources.SourcesRepo`. Connector config is stored
as JSON in the ``sources.config`` column; the helpers here shape it per kind and
return typed views.

Two connectors exist:

- ``imap`` — a mailbox, one row per logical account (``personal``, ``work``).
- ``gcal`` — Google Calendar, a single row per user (``account="google"``)
  holding the OAuth refresh token and which calendars to sync.

Phase 0 provides storage + resolution; the mail-fetch and calendar-sync paths
that consume these land in later phases (see docs/design/per-user-sources.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from prefrontal.crypto import seal, unseal
from prefrontal.memory.store import MemoryStore

#: Connector kinds stored in the ``sources`` table.
IMAP = "imap"
GCAL = "gcal"

#: The single logical account name a Google Calendar source uses per user.
GCAL_ACCOUNT = "google"


@dataclass(frozen=True)
class ImapSource:
    """A resolved IMAP mailbox source, with its password decrypted."""

    account: str
    host: str
    username: str
    password: str
    mailbox: str = "INBOX"
    important_only: bool = False
    retention: str = "signals"
    enabled: bool = True


@dataclass(frozen=True)
class GcalSource:
    """A resolved Google Calendar source, with its refresh token decrypted."""

    refresh_token: str
    calendar_ids: tuple[str, ...]
    namespace: str
    enabled: bool = True


def put_imap_source(
    store: MemoryStore,
    *,
    account: str,
    host: str,
    username: str,
    password: str | None,
    mailbox: str = "INBOX",
    important_only: bool = False,
    retention: str = "signals",
    enabled: bool = True,
) -> int:
    """Create or update a user's IMAP source; return its row id.

    ``password=None`` leaves an existing sealed password untouched (config-only
    edit). Operates on a **scoped** store (the credential lands under that user).
    """
    config = json.dumps(
        {
            "host": host,
            "username": username,
            "mailbox": mailbox,
            "important_only": important_only,
            "retention": retention,
        }
    )
    secret_enc = seal(password) if password is not None else None
    return store.upsert_source(
        kind=IMAP,
        account=account,
        config=config,
        secret_enc=secret_enc,
        enabled=enabled,
    )


def resolve_imap(store: MemoryStore, account: str) -> ImapSource | None:
    """Return the user's IMAP source for ``account`` (password decrypted), or ``None``."""
    row = store.get_source(IMAP, account)
    if row is None:
        return None
    cfg = json.loads(row["config"] or "{}")
    password = unseal(row["secret_enc"]) if row["secret_enc"] is not None else ""
    return ImapSource(
        account=account,
        host=cfg.get("host", ""),
        username=cfg.get("username", ""),
        password=password,
        mailbox=cfg.get("mailbox", "INBOX"),
        important_only=bool(cfg.get("important_only", False)),
        retention=cfg.get("retention", "signals"),
        enabled=bool(row["enabled"]),
    )


def imap_accounts(store: MemoryStore, *, include_disabled: bool = False) -> list[str]:
    """Return the logical account names of the user's IMAP sources."""
    rows = store.list_sources(kind=IMAP, include_disabled=include_disabled)
    return [r["account"] for r in rows]


def put_gcal_source(
    store: MemoryStore,
    *,
    refresh_token: str | None,
    calendar_ids: tuple[str, ...] = ("primary",),
    namespace: str = "gcal",
    enabled: bool = True,
) -> int:
    """Create or update the user's Google Calendar source; return its row id.

    ``refresh_token=None`` leaves an existing sealed token untouched (e.g. when a
    re-consent returns no new refresh token but the calendar selection changed).
    """
    config = json.dumps({"calendar_ids": list(calendar_ids), "namespace": namespace})
    secret_enc = seal(refresh_token) if refresh_token is not None else None
    return store.upsert_source(
        kind=GCAL,
        account=GCAL_ACCOUNT,
        config=config,
        secret_enc=secret_enc,
        enabled=enabled,
    )


def resolve_gcal(store: MemoryStore) -> GcalSource | None:
    """Return the user's Google Calendar source (refresh token decrypted), or ``None``."""
    row = store.get_source(GCAL, GCAL_ACCOUNT)
    if row is None:
        return None
    cfg = json.loads(row["config"] or "{}")
    refresh = unseal(row["secret_enc"]) if row["secret_enc"] is not None else ""
    return GcalSource(
        refresh_token=refresh,
        calendar_ids=tuple(cfg.get("calendar_ids", ["primary"])),
        namespace=cfg.get("namespace", "gcal"),
        enabled=bool(row["enabled"]),
    )
