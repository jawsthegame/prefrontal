"""Service layer over the per-user source registry.

Owns the encryption boundary: callers hand plaintext secrets *in* and get
plaintext *out* here, while the sealed bytes never leave this module and the
:class:`~prefrontal.memory.repos.sources.SourcesRepo`. Connector config is stored
as JSON in the ``sources.config`` column; the helpers here shape it per kind and
return typed views.

The first connector is ``imap`` — a mailbox, one row per logical account
(``personal``, ``work``). A calendar connector (per-user ICS feeds) lands in a
later phase (see docs/design/per-user-sources.md).

Phase 0 provides storage + resolution; the mail-fetch path that consumes these
lands in Phase 1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from prefrontal.config import Settings, get_settings
from prefrontal.crypto import seal, unseal
from prefrontal.mail.imap import ImapAccount
from prefrontal.memory.store import MemoryStore

#: Connector kinds stored in the ``sources`` table.
IMAP = "imap"


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


# -- mail fetch bridge: registry source -> ImapAccount + retention policy ------


@dataclass(frozen=True)
class MailFetchSource:
    """What the mail-fetch path needs for one account: connection + retention."""

    imap: ImapAccount
    policy: str


def resolve_mail_fetch(
    store: MemoryStore, account: str, *, settings: Settings | None = None
) -> MailFetchSource | None:
    """Resolve how to fetch ``account`` for the (scoped) user: DB source, else env.

    Prefers the user's registry ``imap`` source (credentials decrypted, retention
    from its config). Falls back to the global ``MAIL_IMAP_*_<ACCOUNT>`` env +
    :meth:`Settings.policy_for` so a not-yet-migrated deploy keeps working. A
    disabled or credential-incomplete DB source falls through to env rather than
    silently fetching nothing.

    Returns ``None`` when neither a usable DB source nor env credentials exist.
    """
    settings = settings or get_settings()
    src = resolve_imap(store, account)
    if src is not None and src.enabled and src.host and src.username and src.password:
        imap = ImapAccount(
            name=account,
            host=src.host,
            user=src.username,
            password=src.password,
            mailbox=src.mailbox,
            important_only=src.important_only,
        )
        return MailFetchSource(imap=imap, policy=src.retention or "signals")
    imap = ImapAccount.from_env(account)
    if imap is None:
        return None
    return MailFetchSource(imap=imap, policy=settings.policy_for(account))


def mail_fetch_accounts(
    store: MemoryStore, *, settings: Settings | None = None
) -> list[str]:
    """Return the account names to fetch for the (scoped) user.

    The user's enabled ``imap`` registry sources when they have any; otherwise the
    globally-configured ``PREFRONTAL_MAIL_ACCOUNTS`` names — so a user with no
    connected sources still fetches the legacy env-configured accounts.
    """
    settings = settings or get_settings()
    db = imap_accounts(store, include_disabled=False)
    if db:
        return db
    return [name for name, _ in settings.mail_accounts]
