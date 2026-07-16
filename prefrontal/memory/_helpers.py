"""Shared, dependency-free helpers for the memory layer.

Small module-level utilities and constants used by MemoryStore's core and by
the per-domain repository mixins in :mod:`prefrontal.memory.repos`. Kept here
(rather than in store.py) so the mixins can import them without a cycle back
through the store module.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from typing import Any
from urllib.parse import quote_plus

EPISODE_TYPES = ("departure", "task", "checkin", "reminder", "mail", "panic", "switch")
#: Allowed values for ``episodes.outcome``.
OUTCOMES = ("success", "miss", "partial")

#: Coaching-state defaults seeded for every new user at provision time. These
#: used to live as an ``INSERT OR IGNORE`` seed block in ``schema.sql``; with
#: per-user state they are written scoped to each user by :meth:`provision_user`
#: instead, so "a fresh user looks like a fresh install" is one code path.
DEFAULT_COACHING_STATE: tuple[tuple[str, str, str], ...] = (
    ("preferred_briefing_format", "short", "explicit"),
    ("escalation_delay_minutes", "5", "inferred"),
    ("responsive_hours_start", "08:00", "inferred"),
    # 22:00, matching coaching.DEFAULT_RESPONSIVE_END. A stray 14:00 here used to
    # override that code default, so every user's responsive window was 08:00–14:00
    # and *all* non-critical cues were held after 2pm (see the one-time reset in
    # migrate.reset_seeded_responsive_hours_end for existing users).
    ("responsive_hours_end", "22:00", "inferred"),
    ("preferred_reminder_channel", "notification", "inferred"),
    ("time_estimation_bias", "1.4", "inferred"),
    ("active_escalation_path", "notification,sound,tts", "explicit"),
    # Departure-reminder tuning (see prefrontal.modules.departure). Travel time
    # is estimated locally: straight-line distance * road_factor / speed, then
    # padded by time_estimation_bias, an optional distance-relative safety margin
    # (travel_pad_fraction), and a flat prep buffer. travel_pad_fraction is a
    # fraction of the drive (0.15 = +15%), so it scales with distance; 0 = off.
    ("travel_speed_kmh", "30", "inferred"),
    ("travel_road_factor", "1.3", "inferred"),
    ("departure_prep_minutes", "5", "inferred"),
    ("travel_pad_fraction", "0", "inferred"),
    # Master switch for auto-populating travel_pad_fraction from the departure
    # late-rate (prefrontal.memory.patterns). On by default; off freezes the pad.
    ("travel_pad_autolearn", "on", "explicit"),
    ("departure_heads_up_minutes", "30", "inferred"),
    ("departure_soon_minutes", "10", "inferred"),
    # Opt-in network geocoding (Nominatim) for commitment destinations. Off by
    # default: local-first stays the default, and curated `places` + the static
    # `lead_minutes` fallback work without it. Set to '1' to allow the calendar
    # sync to resolve free-text locations to coordinates.
    ("geocoding_enabled", "0", "explicit"),
    # Playbook localization (see prefrontal.clarify). `home_zip` anchors a guided
    # walkthrough's local steps ("find the office serving <zip>"); it's seeded to
    # the deployment's default area and editable per user. `playbook_localization`
    # is the opt-in toggle — OFF by default, so a step's {area} stays a generic
    # phrase until the user turns it on (`prefrontal clarify localize on`).
    ("home_zip", "19027", "explicit"),
    ("playbook_localization", "0", "explicit"),
)


def sha256_hex(token: str) -> str:
    """Return the hex SHA-256 of a token — what we store and compare against.

    Tokens are high-entropy random strings (not human passwords), so a single
    SHA-256 is the right primitive: fast lookups, no plaintext at rest, and no
    need for a slow password hash. The raw token is shown once at creation and
    never stored.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """Mint a fresh, URL-safe access token (shown once, stored only as a hash)."""
    return secrets.token_urlsafe(32)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a :class:`sqlite3.Row` to a ``dict`` (or pass through ``None``)."""
    return dict(row) if row is not None else None


def sql_placeholders(count: int) -> str:
    """A comma-joined run of ``count`` SQL ``?`` placeholders, for an ``IN (…)`` clause."""
    return ",".join("?" * count)


def _safe_close(conn: sqlite3.Connection) -> None:
    """Close a connection, swallowing errors (it may already be closed/broken)."""
    try:
        conn.close()
    except sqlite3.Error:
        pass


# Human labels for known calendar feeds. The feed a calendar commitment came
# from is encoded as the ``external_id`` prefix (``family:UID``); unknown feeds
# fall back to a title-cased slug, so new feeds work without a code change.
_FEED_LABELS = {
    "personal": "Personal",
    "work": "Work",
    "outlook": "Outlook",
    "family": "Family",
}


def feed_label(external_id: str | None) -> str | None:
    """Return a display label for the calendar feed, or ``None`` if not a feed.

    Manual commitments (no namespaced ``external_id``) return ``None``.
    """
    if not external_id or ":" not in external_id:
        return None
    slug = external_id.split(":", 1)[0]
    return _FEED_LABELS.get(slug, slug.capitalize())


def feed_slug(external_id: str | None) -> str | None:
    """Return the calendar feed's namespace slug, or ``None`` for a manual event.

    The raw ``external_id`` prefix (``work:UID`` → ``work``), unmapped and
    lower-level than :func:`feed_label`. It's the stable key a surface uses to
    look up an operator-configured calendar pill (label + color), so the pretty
    label can change without the lookup key moving.
    """
    if not external_id or ":" not in external_id:
        return None
    return external_id.split(":", 1)[0]


def commitment_url(commitment: dict[str, Any]) -> str | None:
    """Return a deeplink to a commitment's source event, or ``None``.

    Prefers an explicit ``source_url`` supplied by the sync (used verbatim, so
    it works for any provider — a Google ``htmlLink``, an Outlook event URL, a
    Gmail message link, …). Otherwise derives a best-effort link for events that
    came from Google Calendar, whose iCal UID ends ``@google.com``: the bare UID
    can't reconstruct a precise event link (Google's ``eid`` also needs the
    calendar id, which an ICS feed doesn't carry), but a title *search* reliably
    lands on the event. Providers we can't derive a link for (Outlook, iCloud)
    return ``None`` unless a ``source_url`` was provided.

    Only ``http(s)`` URLs are returned, so a stored value can be dropped into an
    ``href`` without opening a ``javascript:``-style injection.
    """
    url = (commitment.get("source_url") or "").strip()
    if url:
        return url if url.startswith(("http://", "https://")) else None
    external_id = commitment.get("external_id") or ""
    title = (commitment.get("title") or "").strip()
    if title and external_id.endswith("@google.com"):
        return "https://calendar.google.com/calendar/u/0/r/search?q=" + quote_plus(title)
    return None


def gmail_message_url(message_id: str | None) -> str | None:
    """Return a Gmail deep link to the message with this RFC822 ``Message-ID``.

    Prefrontal stores the message's ``Message-ID`` header (e.g.
    ``<CAF…@mail.gmail.com>``), not a Gmail API id, so we can't build a bare
    ``#all/<id>`` permalink. Gmail's ``rfc822msgid:`` search operator matches
    that header exactly, and searching for it lands on the one message — the
    canonical way to deep-link when all you have is the RFC822 id. The angle
    brackets aren't part of the id, so they're stripped before encoding.

    Returns ``None`` for a missing/blank id. Only used for accounts already
    known to be Gmail (see :meth:`Settings.is_gmail_account`); the result is a
    fixed ``https://`` origin, safe to drop straight into an ``href``.
    """
    mid = (message_id or "").strip().strip("<>").strip()
    if not mid:
        return None
    return "https://mail.google.com/mail/u/0/#search/rfc822msgid:" + quote_plus(mid)


def _with_calendar(d: dict[str, Any]) -> dict[str, Any]:
    """Annotate a commitment dict with calendar label/key and source ``url``.

    ``calendar`` is the human label; ``calendar_key`` is the raw feed slug the
    dashboard uses to look up an operator-configured pill (see
    :attr:`prefrontal.config.Settings.calendar_labels`).
    """
    external_id = d.get("external_id")
    d["calendar"] = feed_label(external_id)
    d["calendar_key"] = feed_slug(external_id)
    d["url"] = commitment_url(d)
    return d
