"""Runtime configuration for Prefrontal.

All settings are read from environment variables (optionally populated from a
local ``.env`` file) so that no secrets or machine-specific paths need to live in
the repository. See ``.env.example`` for the full list of variables and their
defaults.

The single entry point is :func:`get_settings`, which returns a cached
:class:`Settings` instance. Tests and the CLI can call :func:`load_settings`
to force a fresh read of the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Populate ``os.environ`` from a ``.env`` file if one exists.

    This is a deliberately tiny parser so the project has no hard dependency on
    ``python-dotenv``. It supports ``KEY=value`` lines, ignores blanks and
    ``#`` comments, strips surrounding quotes, and never overwrites a variable
    that is already set in the environment.

    Args:
        path: Path to the dotenv file. Missing files are silently ignored.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    """Resolved Prefrontal configuration.

    Attributes:
        db_path: Filesystem path to the SQLite behavioral memory database.
        host: Interface the webhook listener binds to.
        port: TCP port the webhook listener binds to.
        webhook_secret: Operator-bootstrap token. In multi-tenant mode the
            ``X-Prefrontal-Token`` header carries a *per-user* token resolved to
            a user; this shared secret is kept only as a bootstrap operator
            credential — a request bearing it resolves to the first operator user
            until per-user tokens are provisioned. Empty disables the bootstrap.
        default_user: Handle of the user that requests with **no token** resolve
            to (the single-user / trusted-LAN compatibility mode). Empty means a
            token is always required. Documented as LAN-only, like the old
            no-auth mode it replaces.
        n8n_webhook_url: Outbound n8n webhook URL. Empty means the n8n client
            runs in no-op/log mode and nothing leaves the host.
        n8n_webhook_token: Optional token sent to n8n on outbound calls.
        modules: The challenge-area modules to enable (e.g. ``time_blindness``).
            An empty tuple means "enable every registered module" — the right
            default for a fresh install, since everyone's ADHD profile differs
            and modules are opt-out rather than opt-in.
        ollama_url: Base URL of the local Ollama server used by the LLM
            summarizer. Local-first: stays on the host by default.
        ollama_model: Ollama model name the summarizer generates with.
        geocoder_url: Geocoding search endpoint used to resolve a commitment's
            free-text location to coordinates (Nominatim-compatible). Only called
            when the ``geocoding_enabled`` coaching-state flag is on; defaults to
            the public OpenStreetMap Nominatim service.
        geocoder_user_agent: ``User-Agent`` sent with geocoder requests.
            Nominatim's usage policy requires an identifying agent; set it to
            something that identifies your deployment.
        mail_accounts: Per-account retention policy for ingested mail, mapping a
            logical account name to ``"full"`` (store subject/sender/snippet/body)
            or ``"signals"`` (store only subject + sender + the triage verdict;
            bodies are dropped before storage and never sent to the model). An
            account not listed here uses :attr:`mail_default_policy`.
        mail_default_policy: Policy for accounts absent from ``mail_accounts``.
            Defaults to ``"signals"`` — the conservative choice, so an
            unconfigured account never stores message bodies by accident.
    """

    db_path: str = "prefrontal.db"
    host: str = "0.0.0.0"
    port: int = 8000
    webhook_secret: str = ""
    default_user: str = ""
    n8n_webhook_url: str = ""
    n8n_webhook_token: str = ""
    modules: tuple[str, ...] = ()
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    geocoder_url: str = "https://nominatim.openstreetmap.org/search"
    geocoder_user_agent: str = "Prefrontal/0.1 (https://github.com/jawsthegame/prefrontal)"
    mail_accounts: tuple[tuple[str, str], ...] = ()
    mail_default_policy: str = "signals"

    @property
    def auth_enabled(self) -> bool:
        """Whether a token is required to resolve a user.

        Multi-tenant: a request must carry a per-user token unless a
        :attr:`default_user` is configured (the single-user / trusted-LAN
        compatibility mode), in which case a tokenless request resolves to that
        user. Auth is therefore "enforced" exactly when no default user is set.
        """
        return not self.default_user

    @property
    def all_modules_enabled(self) -> bool:
        """Whether every registered module should be enabled (no explicit list)."""
        return not self.modules

    def policy_for(self, account: str) -> str:
        """Return the retention policy for a mail account.

        Args:
            account: The logical account name (e.g. ``"personal"``, ``"corp"``).

        Returns:
            ``"full"`` or ``"signals"`` — the configured policy, or
            :attr:`mail_default_policy` if the account is not configured.
        """
        return dict(self.mail_accounts).get(account, self.mail_default_policy)


def load_settings(dotenv_path: str = ".env") -> Settings:
    """Read configuration from the environment and return a fresh ``Settings``.

    Args:
        dotenv_path: Optional path to a dotenv file to load first.

    Returns:
        A new :class:`Settings` populated from the current environment.
    """
    _load_dotenv(dotenv_path)
    raw_modules = os.environ.get("PREFRONTAL_MODULES", "")
    modules = tuple(m.strip() for m in raw_modules.split(",") if m.strip())
    mail_accounts = _parse_mail_accounts(os.environ.get("PREFRONTAL_MAIL_ACCOUNTS", ""))
    default_policy = os.environ.get("PREFRONTAL_MAIL_DEFAULT_POLICY", "signals").strip()
    if default_policy not in ("full", "signals"):
        default_policy = "signals"
    return Settings(
        db_path=os.environ.get("PREFRONTAL_DB_PATH", "prefrontal.db"),
        host=os.environ.get("PREFRONTAL_HOST", "0.0.0.0"),
        port=int(os.environ.get("PREFRONTAL_PORT", "8000")),
        webhook_secret=os.environ.get("PREFRONTAL_WEBHOOK_SECRET", ""),
        default_user=os.environ.get("PREFRONTAL_DEFAULT_USER", ""),
        n8n_webhook_url=os.environ.get("N8N_WEBHOOK_URL", ""),
        n8n_webhook_token=os.environ.get("N8N_WEBHOOK_TOKEN", ""),
        modules=modules,
        ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
        geocoder_url=os.environ.get(
            "GEOCODER_URL", "https://nominatim.openstreetmap.org/search"
        ),
        geocoder_user_agent=os.environ.get(
            "GEOCODER_USER_AGENT",
            "Prefrontal/0.1 (https://github.com/jawsthegame/prefrontal)",
        ),
        mail_accounts=mail_accounts,
        mail_default_policy=default_policy,
    )


def _parse_mail_accounts(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse ``PREFRONTAL_MAIL_ACCOUNTS`` into ``(account, policy)`` pairs.

    The format is a comma-separated list of ``name=policy`` entries, e.g.
    ``personal=full,work=full,corp=signals``. An entry without ``=`` defaults to
    the ``full`` policy; an unrecognized policy is coerced to ``signals`` (the
    safe default), so a typo never silently starts storing corp bodies.

    Args:
        raw: The raw environment-variable value (may be empty).

    Returns:
        A tuple of ``(account, policy)`` pairs, suitable for
        :attr:`Settings.mail_accounts`.
    """
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, policy = entry.partition("=")
        name = name.strip()
        if not name:
            continue
        policy = policy.strip() if sep else "full"
        if policy not in ("full", "signals"):
            policy = "signals"
        pairs.append((name, policy))
    return tuple(pairs)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings`.

    The first call reads the environment (and ``.env``); subsequent calls return
    the same instance. Call :func:`get_settings.cache_clear` to force a reload.

    Returns:
        The cached :class:`Settings` instance.
    """
    return load_settings()
