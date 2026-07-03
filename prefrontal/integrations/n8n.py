"""n8n workflow-orchestration integration (bidirectional stub).

n8n is Prefrontal's orchestration layer. Two directions matter:

**Outbound** — Prefrontal tells n8n that something happened so a workflow can
run (send a Pushover alert, kick off a TTS escalation, poll mail, ...).
:class:`N8nClient` POSTs a JSON event to a configured n8n webhook URL. If no URL
is configured it runs in no-op/log mode and nothing leaves the host, honoring
the project's "local first" principle.

**Inbound** — n8n pushes an event into Prefrontal. That HTTP surface is the
``POST /webhooks/n8n`` route in :mod:`prefrontal.webhooks.app`; the parsing and
routing helper :func:`parse_inbound_event` lives here so the integration's logic
stays in one place.

Both directions are deliberately minimal stubs with documented TODOs — enough to
wire real workflows against, not a finished feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from prefrontal.config import Settings, get_settings
from prefrontal.log import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class N8nResult:
    """Outcome of an outbound n8n trigger.

    Attributes:
        delivered: ``True`` if the event was actually POSTed to n8n; ``False``
            when running in no-op mode (no URL configured) or on transport error.
        status_code: HTTP status code from n8n, or ``None`` if no request was made.
        detail: Human-readable note about what happened (useful in logs/tests).
    """

    delivered: bool
    status_code: int | None = None
    detail: str = ""


class N8nClient:
    """Outbound client that triggers n8n workflows via a webhook URL.

    Args:
        webhook_url: The n8n webhook URL to POST events to. An empty string puts
            the client in no-op/log mode.
        token: Optional shared secret sent as the ``X-Prefrontal-Token`` header.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self, webhook_url: str = "", token: str = "", timeout: float = 10.0
    ) -> None:
        self.webhook_url = webhook_url
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> N8nClient:
        """Build a client from :class:`~prefrontal.config.Settings`.

        Mirrors :meth:`OllamaClient.from_settings` /
        :meth:`NominatimGeocoder.from_settings` so every integration client has
        the same construction seam: pass an explicit ``Settings`` (tests, the app
        factory) or fall back to the process-wide cached settings.
        """
        resolved = settings or get_settings()
        return cls(
            webhook_url=resolved.n8n_webhook_url,
            token=resolved.n8n_webhook_token,
        )

    @property
    def enabled(self) -> bool:
        """Whether a webhook URL is configured (i.e. outbound calls will fire)."""
        return bool(self.webhook_url)

    def trigger(self, event: str, payload: dict[str, Any] | None = None) -> N8nResult:
        """Send an event to n8n.

        Args:
            event: A short event name n8n can switch on (e.g. ``"episode.logged"``,
                ``"escalation.needed"``).
            payload: Optional JSON-serializable body. ``event`` is merged in under
                the ``"event"`` key.

        Returns:
            An :class:`N8nResult` describing whether and how the event was sent.
            Transport errors are caught and reported rather than raised, so a
            down n8n never breaks a capture path.
        """
        body = {"event": event, **(payload or {})}
        if not self.enabled:
            # No-op mode: the canonical "local first" default. Nothing leaves the box.
            return N8nResult(delivered=False, detail=f"n8n disabled; dropped event '{event}'")

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Prefrontal-Token"] = self.token
        try:
            resp = httpx.post(
                self.webhook_url, json=body, headers=headers, timeout=self.timeout
            )
        except httpx.HTTPError as exc:  # network down, DNS, timeout, ...
            logger.warning("n8n request failed: %s", exc)
            return N8nResult(delivered=False, detail=f"n8n request failed: {exc}")
        return N8nResult(
            delivered=resp.is_success,
            status_code=resp.status_code,
            detail=f"n8n responded {resp.status_code}",
        )


def parse_inbound_event(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize an inbound n8n webhook body into a routing decision.

    This is the stub brain behind ``POST /webhooks/n8n``. Today it only
    classifies the payload; wiring each ``kind`` to a concrete handler is the
    next step.

    Args:
        body: The decoded JSON body n8n sent.

    Returns:
        A dict with at least ``event`` (the event name, defaulting to
        ``"unknown"``) and ``handled`` (always ``False`` for now, since no
        concrete handlers are implemented yet).

    .. todo::
       Route recognized events to real handlers — e.g. an ``episode`` event
       should write to the memory layer, a ``state`` event should update
       coaching state — and set ``handled=True`` accordingly.
    """
    event = str(body.get("event", "unknown"))
    return {"event": event, "handled": False, "payload": body}
