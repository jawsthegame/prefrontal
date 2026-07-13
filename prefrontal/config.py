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

import math
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
        n8n_api_url: Base URL of n8n's Public REST API (e.g.
            ``http://127.0.0.1:5678/api/v1``), used to push the
            ``deploy/n8n/*.json`` workflow templates into the running n8n as
            part of an update. Empty (with no ``n8n_api_key``) means the
            workflow sync is a no-op — nothing is pushed and an update never
            touches n8n.
        n8n_api_key: API key sent as the ``X-N8N-API-KEY`` header on workflow
            sync calls. Both this and ``n8n_api_url`` must be set for the sync
            to run; otherwise it skips cleanly (local-first).
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
            ``work`` → an orange "Acme" pill). Purely cosmetic and operator-set,
            so the account name in the data stays stable while the surface shows a
            friendly name. Accounts absent here render no pill.
        calendar_labels: Per-calendar display labels for the dashboard, the exact
            analogue of ``account_labels`` for commitments: maps a calendar feed
            slug (the ``external_id`` namespace, e.g. ``personal``/``work``/
            ``outlook``) to a ``(label, color)`` pair, so a commitment shows a
            colored pill naming its calendar (e.g. ``work`` → an orange "Acme"
            pill). A feed absent here falls back to its default title-cased label
            with no color.
        timezone: IANA name of the deployment's home timezone (e.g.
            ``"America/New_York"``), used to interpret calendar times that arrive
            *without* their own zone — a floating/naive ICS time or a manual
            commitment. Times that carry an explicit offset or ``TZID`` are
            unaffected (their own zone wins). Defaults to ``"UTC"`` for backward
            compatibility; set it to your actual zone so unzoned events don't land
            hours off.
        calendar_horizon_days: How far ahead calendar sync expands *recurring*
            events (default 30). One-off events ingest at any distance; this only
            bounds how far a weekly/standing series is materialized (a series must
            be bounded), so the calendar page and slot finder see a month out
            instead of ~1 day. Env: ``PREFRONTAL_CALENDAR_HORIZON_DAYS``.
        calendar_min_occurrences: Per-series backstop (default 2). Keep at least this
            many future occurrences of each recurring series even when its next
            instance is beyond ``calendar_horizon_days``, so a monthly/quarterly/annual
            meeting stays visible instead of vanishing between occurrences. Passed to
            ``sync_calendar`` as ``recur_min_occurrences``; 0 disables it.
            Env: ``PREFRONTAL_CALENDAR_MIN_OCCURRENCES``.
        ics_fetch_timeout: Per-request timeout (seconds) for pulling an ICS feed
            over HTTP (default 90). Large/busy corporate calendars can take 30-60s
            for the provider to *generate* their ``.ics``, longer than a tight
            default would wait — so the fetch aborts, the feed ingests nothing, and
            its events go missing. Raise it if a feed still times out.
            Env: ``PREFRONTAL_ICS_TIMEOUT``.
    """

    db_path: str = "prefrontal.db"
    host: str = "0.0.0.0"
    port: int = 8000
    webhook_secret: str = ""
    default_user: str = ""
    n8n_webhook_url: str = ""
    n8n_webhook_token: str = ""
    n8n_api_url: str = ""
    n8n_api_key: str = ""
    modules: tuple[str, ...] = ()
    #: Enabled Context Packs (life-context layers, e.g. ``parent``). Unlike
    #: ``modules`` (empty = all), packs default to **none** — a pack is an explicit
    #: opt-in that switches on modules + seeds vocabulary. See
    #: ``prefrontal.packs`` and ``PREFRONTAL_PACKS``.
    packs: tuple[str, ...] = ()
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    # Optional Claude/Anthropic provider for the dashboard assistant. Local-first:
    # empty key means the assistant uses the local Ollama model. When set, the
    # assistant prefers Claude for natural-language parsing and Ollama remains the
    # fallback. The key never leaves the host except on outbound Anthropic calls.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    # Which *agents* prefer the Anthropic provider over the local model, when a
    # key is configured — the per-agent selectability the README describes. An
    # agent not listed here stays on Ollama regardless of the key. Historical
    # default: only the dashboard ``assistant`` (its long-standing behavior when
    # a key is present). The sentinel ``all`` opts every agent in. Selectable
    # names: assistant, summarizer, briefing, sensor, triage. See
    # ``prefrontal.integrations.provider``.
    anthropic_agents: tuple[str, ...] = ("assistant",)
    geocoder_url: str = "https://nominatim.openstreetmap.org/search"
    geocoder_user_agent: str = "Prefrontal/0.1 (https://github.com/jawsthegame/prefrontal)"
    # Delivery layer — operator *defaults* for the native publishing client
    # (:mod:`prefrontal.integrations.delivery`). Per-user routing in
    # ``coaching_state`` (``ntfy_topic``/``pushover_user_key``/… — multi-tenant
    # §6.5) overrides these; a user with none set falls back here. All empty is
    # the local-first default: the delivery client no-ops and nothing leaves the
    # host until a topic/key is configured.
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    # Optional override for the ntfy push icon (a publicly fetchable PNG/JPEG
    # URL), so a push renders with the PREFRONTAL app icon instead of the generic
    # ntfy glyph. Empty (the default) means the box serves its own icon at
    # ``{oauth_base_url}/brand/app-icon.png`` — the same origin the phone already
    # reaches for the one-tap action buttons — which works for a private
    # deployment. Set this only to point at a differently-hosted image; a per-user
    # ``ntfy_icon`` coaching key overrides it per recipient.
    ntfy_icon: str = ""
    pushover_token: str = ""
    pushover_user_key: str = ""
    # Speak ``voice``-channel nudges aloud on the host via macOS ``say``. Off by
    # default (it only helps when you're at the machine); a per-user
    # ``tts_enabled`` coaching key overrides this.
    tts_enabled: bool = False
    # Twilio (SMS) — used to text a household invite link to a co-parent
    # (:mod:`prefrontal.integrations.sms`). All empty is the local-first default:
    # the SMS client no-ops and nothing is sent until credentials + a from-number
    # are configured. These are operator/account credentials (the same Twilio
    # account the n8n voice-call escalation uses); the recipient number is
    # supplied per invite, not stored here.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""
    # Operator-default recipient for the ``voice``-channel escalation (the outing
    # 150% "your phone rings" nudge the coaching tick now places natively via
    # Twilio, replacing the n8n call node). Empty ⇒ no operator default; a per-user
    # ``twilio_to`` in ``coaching_state`` targets each person's own phone (and, on a
    # multi-user box, is required — the operator default is withheld so one person's
    # call never rings another's phone). The account creds above are shared; only
    # this recipient number is per-user.
    twilio_to: str = ""
    # APNs (native iOS push) — an alternative to ntfy for the native-app users
    # who register a device token (per-user ``apns_token`` in ``coaching_state``).
    # Token-based auth: a .p8 key (its PEM contents), its key id, and the team id;
    # ``apns_topic`` is the app bundle id. These are operator/account credentials
    # (shared), like the Twilio account — only the device token is per-user. Empty
    # ⇒ APNs off, and delivery falls back to ntfy. See docs/multi-tenant.md.
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_auth_key: str = ""   # the .p8 private key PEM contents
    apns_topic: str = "com.morningstatic.prefrontal"
    apns_use_sandbox: bool = False   # true → api.sandbox.push.apple.com (dev builds)
    mail_accounts: tuple[tuple[str, str], ...] = ()
    mail_default_policy: str = "signals"
    #: Logical account names that are Gmail inboxes (resolved from IMAP host
    #: config at load time; see :func:`prefrontal.mail.imap.gmail_account_names`).
    #: Surfaces use this to decide whether a todo can deep-link to its source mail.
    gmail_accounts: frozenset[str] = frozenset()
    account_labels: tuple[tuple[str, str, str], ...] = ()
    calendar_labels: tuple[tuple[str, str, str], ...] = ()
    # Work/life guardrail: map a mail account to a life *domain* (work / home / …)
    # so a todo created from that inbox inherits the domain's time band and never
    # squeezes into the wrong part of the day. Empty (default) → no domain is
    # stamped and scheduling behaves exactly as before. See `account_domain_map`.
    account_domains: tuple[tuple[str, str], ...] = ()
    timezone: str = "UTC"
    # How far ahead calendar sync materializes *recurring* events, in days (default
    # 30). One-off events ingest at any distance; this only bounds how far a weekly/
    # standing series is expanded, so the calendar page and slot finder see a month
    # out rather than ~1 day. The CLI/webhook sync pass it to `sync_calendar`
    # as `recur_horizon_hours`. See `prefrontal.commitments.RECUR_HORIZON_HOURS`.
    calendar_horizon_days: float = 30.0
    # Per-series backstop for recurring events: the minimum number of *future*
    # occurrences to keep for each series even when its next instance falls beyond
    # `calendar_horizon_days`. Without it, a meeting that recurs less often than the
    # horizon (monthly/quarterly/annual) is invisible between occurrences — its next
    # instance is past the window. This reaches past the horizon per-series until it
    # has this many future occurrences, so long-interval meetings stay visible
    # without widening the horizon for *every* series (which would materialize daily/
    # weekly runs months out). 0 disables it. Env: `PREFRONTAL_CALENDAR_MIN_OCCURRENCES`.
    # See `prefrontal.commitments.expand_recurrences`.
    calendar_min_occurrences: int = 2
    # Per-request timeout (seconds) for fetching an ICS feed. A big/busy corporate
    # calendar can take 30-60s for the provider to render its `.ics`; a tight
    # timeout aborts the fetch, the feed ingests nothing, and its events vanish.
    # Env: `PREFRONTAL_ICS_TIMEOUT`. See `prefrontal.ics.fetch_ics`.
    ics_fetch_timeout: float = 90.0
    # Suggestion time windows: when a todo may be proposed into free time. The
    # off-zone is a hard local band nothing is ever suggested inside (default
    # 22:00-06:00 overnight); `todo_windows` maps a todo *category* or *source*
    # to a narrower "HH:MM-HH:MM" band (e.g. work=09:00-17:00). Empty values fall
    # back to the built-in defaults in `prefrontal.scheduling`; coaching-state may
    # override per user at runtime. See `WindowConfig.build`.
    todo_offzone: str = ""
    todo_windows: tuple[tuple[str, str], ...] = ()
    # Triage learns from dropped email todos (see prefrontal/mail/feedback.py). A
    # drop only counts as a "this didn't need action" correction when it's quick
    # (dropped within this many days of arriving) or comes from a sender dropped
    # at least `triage_repeat_threshold` times — so a one-off slow drop, which is
    # more likely avoidance than a triage error, is ignored.
    triage_quick_drop_days: float = 2.0
    triage_repeat_threshold: int = 2
    # Triage agent (docs/triage-agent.md §8). triage_use_llm=False skips the model
    # refinement of ambiguous signals (pure heuristic — useful when Ollama is busy
    # or absent). triage_drop_threshold: below this confidence a "noise" verdict is
    # surfaced instead of dropped, so uncertain noise is seen rather than discarded.
    triage_use_llm: bool = True
    triage_drop_threshold: float = 0.0
    # Google sign-in for the web surfaces (dashboard/family). Machine clients
    # (n8n, iOS Shortcuts, the widget) keep using per-user tokens regardless.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    oauth_base_url: str = ""        # public https origin, e.g. https://mac-mini.tailnet.ts.net
    session_secret: str = ""        # HMAC key signing the browser session cookie
    google_oauth_allowed: str = ""  # "email=handle,email2=handle2" allowlist
    # Fernet key sealing per-user source secrets (IMAP passwords, and later ICS
    # feed URLs) in the `sources` table. Supply inline (secret_key) or via a
    # keyfile path (secret_key_file); losing it makes sealed secrets
    # unrecoverable. Mint one with `prefrontal secrets init`. See
    # prefrontal/crypto.py.
    secret_key: str = ""
    secret_key_file: str = ""
    # Retired Fernet keys, accepted for *decrypt only* (comma-separated), so the
    # primary secret_key can be rotated without every sealed secret breaking at
    # once: new seals use secret_key, old secrets keep opening under a retired key
    # until they're re-sealed. See prefrontal/crypto.py.
    secret_keys_old: tuple[str, ...] = ()
    # Remote self-update: pull the latest code + restart the service from an
    # operator HTTP call (POST /admin/update) or the CLI (`prefrontal update`).
    # Powerful (it runs whatever is on the branch), so the HTTP surface is OFF
    # until self_update_enabled is set; the CLI runs on-host and is always allowed.
    self_update_enabled: bool = False  # gate POST /admin/update|restart
    self_update_repo_dir: str = ""     # where to run git (default: the repo root)
    update_cmd: str = ""               # override update cmd (default: bash deploy/update.sh)
    restart_cmd: str = ""              # override restart cmd (default: launchd kickstart)

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

    def is_gmail_account(self, account: str | None) -> bool:
        """Whether ``account`` is a Gmail inbox (so its todos can deep-link to mail).

        Backed by :attr:`gmail_accounts`, resolved from IMAP host config at load
        time. ``None``/unknown accounts (manual and impulse todos) are not Gmail.
        """
        return bool(account) and account in self.gmail_accounts

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

    @property
    def account_domain_map(self) -> dict[str, str]:
        """Mail account name → life domain (``work``/``home``/…), lowercased.

        Built from :attr:`account_domains` (``PREFRONTAL_ACCOUNT_DOMAINS``). Empty
        (the default) means no todo gets a domain from its mailbox, so scheduling
        is unchanged until an operator opts in. The domain resolves to a time band
        via ``PREFRONTAL_TODO_WINDOWS`` / the built-in category windows.
        """
        return {account: domain for account, domain in self.account_domains}

    @property
    def calendar_label_map(self) -> dict[str, dict[str, str]]:
        """Calendar feed slug → ``{"label", "color"}`` for dashboard pills.

        Built from :attr:`calendar_labels`. An empty map (the default) means
        commitments keep their default calendar labels with no color, so the
        dashboard behaves exactly as before until an operator configures
        ``PREFRONTAL_CALENDAR_LABELS``.
        """
        return {
            feed: {"label": label, "color": color}
            for feed, label, color in self.calendar_labels
        }

    @property
    def todo_window_map(self) -> dict[str, str]:
        """Category/source key → ``"HH:MM-HH:MM"`` window, from :attr:`todo_windows`.

        Fed to :meth:`prefrontal.scheduling.WindowConfig.build` as the env layer.
        An empty map (the default) means only the built-in category defaults apply
        until an operator sets ``PREFRONTAL_TODO_WINDOWS``.
        """
        return {key: spec for key, spec in self.todo_windows}


def _int_env(name: str, default: int) -> int:
    """Read an integer env var, falling back to ``default`` if absent or malformed.

    Matches the forgiving spirit of the string parsers here: a fat-fingered value
    (``PREFRONTAL_PORT=eight``) degrades to the default rather than raising a
    ``ValueError`` that takes the whole process down at startup.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    """Read a float env var, falling back to ``default`` if absent or malformed.

    The float counterpart to :func:`_int_env` (see its rationale).
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    # float() parses "nan"/"inf" — but a non-finite threshold silently breaks
    # comparisons (every `x < nan` is False), so treat it as malformed too.
    return value if math.isfinite(value) else default


def _read_apns_auth_key() -> str:
    """The APNs .p8 signing key PEM, from ``APNS_AUTH_KEY`` or ``APNS_AUTH_KEY_PATH``.

    Accepts the key inline (``APNS_AUTH_KEY`` — ``\\n`` escapes are turned back
    into newlines so it survives a single-line ``.env``) or a path to the ``.p8``
    file (``APNS_AUTH_KEY_PATH``). Returns ``""`` if neither is set or the path
    can't be read, which leaves APNs disabled (delivery falls back to ntfy).
    """
    inline = os.environ.get("APNS_AUTH_KEY", "")
    if inline.strip():
        return inline.replace("\\n", "\n").strip()
    path = os.environ.get("APNS_AUTH_KEY_PATH", "").strip()
    if path:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return ""
    return ""


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
    raw_packs = os.environ.get("PREFRONTAL_PACKS", "")
    packs = tuple(p.strip() for p in raw_packs.split(",") if p.strip())
    mail_accounts = _parse_mail_accounts(os.environ.get("PREFRONTAL_MAIL_ACCOUNTS", ""))
    account_labels = _parse_account_labels(
        os.environ.get("PREFRONTAL_ACCOUNT_LABELS", "")
    )
    calendar_labels = _parse_calendar_labels(
        os.environ.get("PREFRONTAL_CALENDAR_LABELS", "")
    )
    account_domains = _parse_account_domains(
        os.environ.get("PREFRONTAL_ACCOUNT_DOMAINS", "")
    )
    default_policy = os.environ.get("PREFRONTAL_MAIL_DEFAULT_POLICY", "signals").strip()
    if default_policy not in ("full", "signals"):
        default_policy = "signals"
    # Classify the configured account universe (retention policies + label pills)
    # into Gmail vs not, so surfaces can deep-link Gmail-sourced todos.
    from prefrontal.mail.imap import gmail_account_names

    account_names = {a for a, _ in mail_accounts} | {a for a, _, _ in account_labels}
    gmail_accounts = gmail_account_names(tuple(account_names))
    return Settings(
        db_path=os.environ.get("PREFRONTAL_DB_PATH", "prefrontal.db"),
        host=os.environ.get("PREFRONTAL_HOST", "0.0.0.0"),
        port=_int_env("PREFRONTAL_PORT", 8000),
        webhook_secret=os.environ.get("PREFRONTAL_WEBHOOK_SECRET", ""),
        default_user=os.environ.get("PREFRONTAL_DEFAULT_USER", ""),
        n8n_webhook_url=os.environ.get("N8N_WEBHOOK_URL", ""),
        n8n_webhook_token=os.environ.get("N8N_WEBHOOK_TOKEN", ""),
        n8n_api_url=os.environ.get("N8N_API_URL", "").rstrip("/"),
        n8n_api_key=os.environ.get("N8N_API_KEY", ""),
        modules=modules,
        packs=packs,
        ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8"),
        anthropic_agents=_parse_anthropic_agents(os.environ.get("ANTHROPIC_AGENTS")),
        geocoder_url=os.environ.get(
            "GEOCODER_URL", "https://nominatim.openstreetmap.org/search"
        ),
        geocoder_user_agent=os.environ.get(
            "GEOCODER_USER_AGENT",
            "Prefrontal/0.1 (https://github.com/jawsthegame/prefrontal)",
        ),
        ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        or "https://ntfy.sh",
        ntfy_topic=os.environ.get("NTFY_TOPIC", ""),
        ntfy_token=os.environ.get("NTFY_TOKEN", ""),
        ntfy_icon=os.environ.get("NTFY_ICON", ""),
        pushover_token=os.environ.get("PUSHOVER_TOKEN", ""),
        pushover_user_key=os.environ.get("PUSHOVER_USER_KEY", ""),
        tts_enabled=os.environ.get("PREFRONTAL_TTS_ENABLED", "").strip().lower()
        in ("1", "true", "yes", "on"),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", "").strip(),
        twilio_from=os.environ.get("TWILIO_FROM", "").strip(),
        twilio_to=os.environ.get("TWILIO_TO", "").strip(),
        apns_key_id=os.environ.get("APNS_KEY_ID", "").strip(),
        apns_team_id=os.environ.get("APNS_TEAM_ID", "").strip(),
        # Accept the .p8 contents inline (APNS_AUTH_KEY) or a path to it
        # (APNS_AUTH_KEY_PATH); \n-escapes in the inline form are unescaped.
        apns_auth_key=_read_apns_auth_key(),
        apns_topic=os.environ.get("APNS_TOPIC", "com.morningstatic.prefrontal").strip(),
        apns_use_sandbox=os.environ.get("APNS_USE_SANDBOX", "").strip().lower()
        in ("1", "true", "yes", "on"),
        mail_accounts=mail_accounts,
        mail_default_policy=default_policy,
        gmail_accounts=gmail_accounts,
        account_labels=account_labels,
        calendar_labels=calendar_labels,
        account_domains=account_domains,
        timezone=os.environ.get("PREFRONTAL_TIMEZONE", "UTC").strip() or "UTC",
        calendar_horizon_days=_float_env("PREFRONTAL_CALENDAR_HORIZON_DAYS", 30.0),
        calendar_min_occurrences=_int_env("PREFRONTAL_CALENDAR_MIN_OCCURRENCES", 2),
        ics_fetch_timeout=_float_env("PREFRONTAL_ICS_TIMEOUT", 90.0),
        todo_offzone=os.environ.get("PREFRONTAL_TODO_OFFZONE", "").strip(),
        todo_windows=_parse_todo_windows(os.environ.get("PREFRONTAL_TODO_WINDOWS", "")),
        triage_quick_drop_days=_float_env("PREFRONTAL_TRIAGE_QUICK_DROP_DAYS", 2.0),
        triage_repeat_threshold=_int_env("PREFRONTAL_TRIAGE_REPEAT_THRESHOLD", 2),
        triage_use_llm=os.environ.get("PREFRONTAL_TRIAGE_LLM", "true").strip().lower()
        not in ("0", "false", "no", "off"),
        triage_drop_threshold=_float_env("PREFRONTAL_TRIAGE_DROP", 0.0),
        google_oauth_client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        google_oauth_client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        oauth_base_url=os.environ.get("OAUTH_BASE_URL", "").rstrip("/"),
        session_secret=os.environ.get("SESSION_SECRET", ""),
        google_oauth_allowed=os.environ.get("GOOGLE_OAUTH_ALLOWED", ""),
        secret_key=os.environ.get("PREFRONTAL_SECRET_KEY", "").strip(),
        secret_key_file=os.environ.get("PREFRONTAL_SECRET_KEY_FILE", "").strip(),
        secret_keys_old=tuple(
            k.strip()
            for k in os.environ.get("PREFRONTAL_SECRET_KEYS_OLD", "").split(",")
            if k.strip()
        ),
        self_update_enabled=os.environ.get("PREFRONTAL_SELF_UPDATE", "").strip().lower()
        in ("1", "true", "yes", "on"),
        self_update_repo_dir=os.environ.get("PREFRONTAL_REPO_DIR", "").strip(),
        update_cmd=os.environ.get("PREFRONTAL_UPDATE_CMD", "").strip(),
        restart_cmd=os.environ.get("PREFRONTAL_RESTART_CMD", "").strip(),
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


def _parse_label_pills(raw: str) -> tuple[tuple[str, str, str], ...]:
    """Parse a ``key=label:color`` pill spec into ``(key, label, color)`` triples.

    The format is a comma-separated list of ``key=label:color`` entries, e.g.
    ``work=Acme:orange,outlook=telco:magenta``. The ``:color`` part is
    optional (``key=label`` yields an empty color, letting the surface pick a
    default), and the label may itself contain ``:`` — the color is split from the
    last colon. Entries without ``=`` or with an empty key/label are skipped, so a
    malformed value degrades to "no pill" rather than raising.

    Shared by the mail-account (:func:`_parse_account_labels`) and calendar-feed
    (:func:`_parse_calendar_labels`) pill configs, which differ only in what the
    key means.

    Args:
        raw: The raw environment-variable value (may be empty).

    Returns:
        A tuple of ``(key, label, color)`` triples.
    """
    out: list[tuple[str, str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, _, spec = entry.partition("=")
        key = key.strip()
        label, sep, color = spec.rpartition(":")
        if not sep:  # no ":" — the whole spec is the label, color unset
            label, color = spec, ""
        label, color = label.strip(), color.strip()
        if key and label:
            out.append((key, label, color))
    return tuple(out)


def _parse_account_labels(raw: str) -> tuple[tuple[str, str, str], ...]:
    """Parse ``PREFRONTAL_ACCOUNT_LABELS`` into ``(account, label, color)`` triples.

    See :func:`_parse_label_pills` for the format; here the key is the logical
    mail account name from ``PREFRONTAL_MAIL_ACCOUNTS``.
    """
    return _parse_label_pills(raw)


def _parse_calendar_labels(raw: str) -> tuple[tuple[str, str, str], ...]:
    """Parse ``PREFRONTAL_CALENDAR_LABELS`` into ``(feed, label, color)`` triples.

    See :func:`_parse_label_pills` for the format; here the key is the calendar
    feed slug — the ``external_id`` namespace a synced commitment carries (e.g.
    ``personal``, ``work``, ``outlook``).
    """
    return _parse_label_pills(raw)


def _parse_todo_windows(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse ``PREFRONTAL_TODO_WINDOWS`` into ``(key, "HH:MM-HH:MM")`` pairs.

    The format is a comma-separated list of ``key=HH:MM-HH:MM`` entries, e.g.
    ``work=09:00-17:00,home=06:00-22:00``. The key is a todo category or source.
    Entries without ``=`` or with an empty key are skipped; the *value* is passed
    through verbatim (validated later by
    :func:`prefrontal.scheduling.parse_window`), so a malformed range degrades to
    "use the default" rather than raising here.

    Args:
        raw: The raw environment-variable value (may be empty).

    Returns:
        A tuple of ``(key, window_spec)`` pairs, suitable for
        :attr:`Settings.todo_windows`.
    """
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, _, spec = entry.partition("=")
        key, spec = key.strip().lower(), spec.strip()
        if key and spec:
            pairs.append((key, spec))
    return tuple(pairs)


def _parse_anthropic_agents(raw: str | None) -> tuple[str, ...]:
    """Parse ``ANTHROPIC_AGENTS`` into the set of agents that prefer Claude.

    Unset (``None``) keeps the historical default — only the dashboard
    ``assistant`` uses Claude when a key is configured. An explicit *empty*
    string opts every agent back to the local model. The sentinel ``all`` (or
    ``*``) opts them all in. Otherwise it's a comma-separated list of agent
    names, lowercased and de-duplicated; unknown names are harmless (they simply
    never match a real agent), so a typo degrades to "stay local".

    Args:
        raw: The raw environment-variable value, or ``None`` if unset.

    Returns:
        A tuple of agent names (possibly the ``("all",)`` sentinel), suitable
        for :attr:`Settings.anthropic_agents`.
    """
    if raw is None:
        return ("assistant",)
    out: list[str] = []
    for entry in raw.split(","):
        token = entry.strip().lower()
        if not token:
            continue
        if token == "*":
            token = "all"
        if token not in out:
            out.append(token)
    return tuple(out)


def _parse_account_domains(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse ``PREFRONTAL_ACCOUNT_DOMAINS`` into ``(account, domain)`` pairs.

    Format: comma-separated ``account=domain`` entries, e.g.
    ``work@co=work,me@gmail=home``. The key is the logical mail account name
    (from ``PREFRONTAL_MAIL_ACCOUNTS``); the domain is a life sphere that resolves
    to a time band. Entries without ``=`` or an empty side are skipped; the domain
    is lowercased so it matches the (lowercased) window keys.
    """
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        account, _, domain = entry.partition("=")
        account, domain = account.strip(), domain.strip().lower()
        if account and domain:
            pairs.append((account, domain))
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
