"""Service layer over the per-user source registry.

Owns the encryption boundary: callers hand plaintext secrets *in* and get
plaintext *out* here, while the sealed bytes never leave this module and the
:class:`~prefrontal.memory.repos.sources.SourcesRepo`. Connector config is stored
as JSON in the ``sources.config`` column; the helpers here shape it per kind and
return typed views.

Two connectors exist:

- ``imap`` — a mailbox, one row per logical account (``personal``, ``work``);
  the sealed secret is the IMAP password.
- ``ics`` — a private calendar feed, one row per feed (slug = account); the
  sealed secret is the feed URL (a bearer secret — anyone with it can read the
  calendar), and config carries the user's own addresses for declined-filtering.

See docs/design/per-user-sources.md.
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
ICS = "ics"


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


# -- calendar (private ICS feeds) --------------------------------------------


@dataclass(frozen=True)
class IcsSource:
    """A resolved ICS calendar feed, with its URL decrypted."""

    account: str  # feed slug, also the external_id namespace
    url: str
    namespace: str
    me_emails: tuple[str, ...] = ()
    label: str | None = None
    enabled: bool = True


def put_ics_source(
    store: MemoryStore,
    *,
    account: str,
    url: str | None,
    namespace: str | None = None,
    me_emails: tuple[str, ...] = (),
    label: str | None = None,
    enabled: bool = True,
) -> int:
    """Create or update a user's ICS calendar source; return its row id.

    The feed ``url`` is a bearer secret and is sealed at rest. ``url=None`` on an
    update leaves the existing sealed URL untouched (config-only edit).
    ``namespace`` defaults to ``account`` (the ``external_id`` feed slug).
    """
    config = json.dumps(
        {
            "namespace": (namespace or account),
            "me_emails": list(me_emails),
            "label": label,
        }
    )
    secret_enc = seal(url) if url is not None else None
    return store.upsert_source(
        kind=ICS,
        account=account,
        config=config,
        secret_enc=secret_enc,
        enabled=enabled,
    )


def ics_sources(
    store: MemoryStore, *, include_disabled: bool = False
) -> list[IcsSource]:
    """Return the user's ICS calendar sources, with feed URLs decrypted."""
    out: list[IcsSource] = []
    for row in store.list_sources(kind=ICS, include_disabled=include_disabled):
        cfg = json.loads(row["config"] or "{}")
        url = unseal(row["secret_enc"]) if row["secret_enc"] is not None else ""
        out.append(
            IcsSource(
                account=row["account"],
                url=url,
                namespace=cfg.get("namespace") or row["account"],
                me_emails=tuple(cfg.get("me_emails", [])),
                label=cfg.get("label"),
                enabled=bool(row["enabled"]),
            )
        )
    return out


# -- mail fetch bridge: registry source -> ImapAccount + retention policy ------


@dataclass(frozen=True)
class MailFetchSource:
    """What the mail-fetch path needs for one account: connection + retention."""

    imap: ImapAccount
    policy: str


def resolve_mail_fetch(
    store: MemoryStore,
    account: str,
    *,
    settings: Settings | None = None,
    allow_env_fallback: bool = True,
) -> MailFetchSource | None:
    """Resolve how to fetch ``account`` for the (scoped) user: DB source, else env.

    Prefers the user's registry ``imap`` source (credentials decrypted, retention
    from its config). Falls back to the global ``MAIL_IMAP_*_<ACCOUNT>`` env +
    :meth:`Settings.policy_for` so a not-yet-migrated single-user deploy keeps
    working. A disabled or credential-incomplete DB source falls through to env
    rather than silently fetching nothing.

    ``allow_env_fallback=False`` disables the env path — required for a
    ``--all-users`` fan-out, where the global env config belongs to *one* mailbox:
    letting every source-less user inherit it would fetch that one inbox into
    everyone's scope (the multi-tenant leak). There, a user with no DB source
    resolves to ``None`` and is skipped.

    Returns ``None`` when no usable source applies.
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
    if not allow_env_fallback:
        return None
    imap = ImapAccount.from_env(account)
    if imap is None:
        return None
    return MailFetchSource(imap=imap, policy=settings.policy_for(account))


def mail_fetch_accounts(
    store: MemoryStore,
    *,
    settings: Settings | None = None,
    allow_env_fallback: bool = True,
) -> list[str]:
    """Return the account names to fetch for the (scoped) user.

    The user's enabled ``imap`` registry sources when they have any; otherwise,
    when ``allow_env_fallback`` is set, the globally-configured
    ``PREFRONTAL_MAIL_ACCOUNTS`` names (so a not-yet-migrated single-user deploy
    still fetches). With ``allow_env_fallback=False`` (a ``--all-users`` fan-out)
    a source-less user returns ``[]`` — the global env mailbox is one person's and
    must not be fetched into everyone's scope.
    """
    settings = settings or get_settings()
    db = imap_accounts(store, include_disabled=False)
    if db:
        return db
    if not allow_env_fallback:
        return []
    return [name for name, _ in settings.mail_accounts]
