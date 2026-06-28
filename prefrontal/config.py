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
        webhook_secret: Shared secret expected in the ``X-Prefrontal-Token``
            header on inbound webhooks. An empty string disables auth (only safe
            on a fully trusted local network).
        n8n_webhook_url: Outbound n8n webhook URL. Empty means the n8n client
            runs in no-op/log mode and nothing leaves the host.
        n8n_webhook_token: Optional token sent to n8n on outbound calls.
        modules: The challenge-area modules to enable (e.g. ``time_blindness``).
            An empty tuple means "enable every registered module" — the right
            default for a fresh install, since everyone's ADHD profile differs
            and modules are opt-out rather than opt-in.
    """

    db_path: str = "prefrontal.db"
    host: str = "0.0.0.0"
    port: int = 8000
    webhook_secret: str = ""
    n8n_webhook_url: str = ""
    n8n_webhook_token: str = ""
    modules: tuple[str, ...] = ()

    @property
    def auth_enabled(self) -> bool:
        """Whether inbound webhook authentication is enforced."""
        return bool(self.webhook_secret)

    @property
    def all_modules_enabled(self) -> bool:
        """Whether every registered module should be enabled (no explicit list)."""
        return not self.modules


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
    return Settings(
        db_path=os.environ.get("PREFRONTAL_DB_PATH", "prefrontal.db"),
        host=os.environ.get("PREFRONTAL_HOST", "0.0.0.0"),
        port=int(os.environ.get("PREFRONTAL_PORT", "8000")),
        webhook_secret=os.environ.get("PREFRONTAL_WEBHOOK_SECRET", ""),
        n8n_webhook_url=os.environ.get("N8N_WEBHOOK_URL", ""),
        n8n_webhook_token=os.environ.get("N8N_WEBHOOK_TOKEN", ""),
        modules=modules,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings`.

    The first call reads the environment (and ``.env``); subsequent calls return
    the same instance. Call :func:`get_settings.cache_clear` to force a reload.

    Returns:
        The cached :class:`Settings` instance.
    """
    return load_settings()
