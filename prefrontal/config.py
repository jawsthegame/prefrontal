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
        account_labels: Per-account display labels for the dashboard, mapping a
            logical account name to a ``(label, color)`` pair — so a todo that
            came from mail shows a colored pill naming the real account (e.g.
            ``work`` → an orange "Vistar" pill). Purely cosmetic and operator-set,
            so the account name in the data stays stable while the surface shows a
            friendly name. Accounts absent here render no pill.
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
    account_labels: tuple[tuple[str, str, str], ...] = ()
    # Triage learns from dropped email todos (see prefrontal/mail/feedback.py). A
    # drop only counts as a "this didn't need action" correction when it's quick
    # (dropped within this many days of arriving) or comes from a sender dropped
    # at least `triage_repeat_threshold` times — so a one-off slow drop, which is
    # more likely avoidance than a triage error, is ignored.
    triage_quick_drop_days: float = 2.0
    triage_repeat_threshold: int = 2
    # Google sign-in for the web surfaces (dashboard/family). Machine clients
    # (n8n, iOS Shortcuts, the widget) keep using per-user tokens regardless.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    oauth_base_url: str = ""        # public https origin, e.g. https://agent-1.tail8b0a.ts.net
    session_secret: str = ""        # HMAC key signing the browser session cookie
    google_oauth_allowed: str = ""  # "email=handle,email2=handle2" allowlist

    @property
    def google_oauth_enabled(self) -> bool:
        """Whether Google sign-in is fully configured (else the login route 404s)."""
        return bool(
            self.google_oauth_client_id
            and self.google_oauth_client_secret
            and self.oauth_base_url
            and self.session_secret
        )

    @property
    def oauth_allowed_emails(self) -> dict[str, str]:
        """Parsed ``email -> user handle`` allowlist (lowercased emails)."""
        out: dict[str, str] = {}
        for entry in self.google_oauth_allowed.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            email, _, handle = entry.partition("=")
            email, handle = email.strip().lower(), handle.strip()
            if email and handle:
                out[email] = handle
        return out

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

    @property
    def account_label_map(self) -> dict[str, dict[str, str]]:
        """Account name → ``{"label", "color"}`` for dashboard pills.

        Built from :attr:`account_labels`. An empty map (the default) means no
        account pills are shown, so the dashboard behaves exactly as before until
        an operator configures ``PREFRONTAL_ACCOUNT_LABELS``.
        """
        return {
            account: {"label": label, "color": color}
            for account, label, color in self.account_labels
        }


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
    account_labels = _parse_account_labels(
        os.environ.get("PREFRONTAL_ACCOUNT_LABELS", "")
    )
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
        account_labels=account_labels,
        triage_quick_drop_days=float(
            os.environ.get("PREFRONTAL_TRIAGE_QUICK_DROP_DAYS", "2")
        ),
        triage_repeat_threshold=int(
            os.environ.get("PREFRONTAL_TRIAGE_REPEAT_THRESHOLD", "2")
        ),
        google_oauth_client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        google_oauth_client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        oauth_base_url=os.environ.get("OAUTH_BASE_URL", "").rstrip("/"),
        session_secret=os.environ.get("SESSION_SECRET", ""),
        google_oauth_allowed=os.environ.get("GOOGLE_OAUTH_ALLOWED", ""),
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


def _parse_account_labels(raw: str) -> tuple[tuple[str, str, str], ...]:
    """Parse ``PREFRONTAL_ACCOUNT_LABELS`` into ``(account, label, color)`` triples.

    The format is a comma-separated list of ``account=label:color`` entries, e.g.
    ``work=Vistar:orange,outlook=t-mobile:magenta``. The ``:color`` part is
    optional (``account=label`` yields an empty color, letting the surface pick a
    default), and the label may itself contain ``:`` — the color is split from the
    last colon. Entries without ``=`` or with an empty account/label are skipped,
    so a malformed value degrades to "no pill" rather than raising.

    Args:
        raw: The raw environment-variable value (may be empty).

    Returns:
        A tuple of ``(account, label, color)`` triples, suitable for
        :attr:`Settings.account_labels`.
    """
    out: list[tuple[str, str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        account, _, spec = entry.partition("=")
        account = account.strip()
        label, sep, color = spec.rpartition(":")
        if not sep:  # no ":" — the whole spec is the label, color unset
            label, color = spec, ""
        label, color = label.strip(), color.strip()
        if account and label:
            out.append((account, label, color))
    return tuple(out)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings`.

    The first call reads the environment (and ``.env``); subsequent calls return
    the same instance. Call :func:`get_settings.cache_clear` to force a reload.

    Returns:
        The cached :class:`Settings` instance.
    """
    return load_settings()
