"""First-class delivery client — publish a coaching :class:`Decision` for real.

The coaching agent (:mod:`prefrontal.coaching`) decides *what to say, when, and
on which channel class* (``digest``/``push``/``sound``/``voice`` — see
:data:`~prefrontal.coaching.CHANNEL_LADDER`). Until now the actual publish was
done by an n8n node that read the ``actions`` a nudge endpoint returned and
POSTed them to ntfy. This module is the native Python replacement: given a
:class:`~prefrontal.coaching.Decision` and the acting user's per-user *routing*
(their ntfy topic / Pushover key), it maps the channel class to a concrete
transport and publishes — with ntfy's inline ``http`` action buttons attached so
a nudge stays one background tap.

Local-first, like every other integration client (:mod:`prefrontal.integrations`):
a transport with nothing configured (no ntfy topic, no Pushover credentials)
runs in **no-op/log mode** and nothing leaves the host. Errors are caught and
reported as a :class:`DeliveryResult`, never raised — a down transport must not
sink the coaching tick, exactly as :class:`~prefrontal.integrations.n8n.N8nClient`
swallows transport errors on the capture path.

Channel classes map to transports as:

===========  ==================================================================
``digest``   held — folds into the morning briefing, never interrupts (no send)
``push``     ntfy (default priority) or Pushover
``sound``    ntfy (high priority) or Pushover (high)
``voice``    local TTS when enabled, else ntfy (max/urgent) / Pushover (high)
===========  ==================================================================

**Suppression and debounce are not re-done here.** The engine's
:func:`~prefrontal.coaching.suppressed` already gated the ``Decision`` (quiet
hours + per-``dedup_key`` debounce) and :func:`~prefrontal.coaching.record_fired`
stamps it. This layer only *routes and sends*. Per-user routing identifiers live
in ``coaching_state`` (``ntfy_topic``/``pushover_user_key``/… — the multi-tenant
spec's §6.5 delivery fields), falling back to the operator defaults in
:class:`~prefrontal.config.Settings` so a single-user box needs no per-user setup.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import httpx

from prefrontal.config import Settings, get_settings
from prefrontal.webhooks.notify import nudge_actions

if TYPE_CHECKING:  # only a type annotation — importing it at runtime cycles via
    # coaching → scheduling → todos → integrations (this package).
    from prefrontal.coaching import Decision

#: Notification title every channel shares, so pushes read as one assistant.
_TITLE = "Prefrontal"

#: Channel class → ntfy priority (1 min … 5 max/urgent). ``digest`` is quiet.
NTFY_PRIORITY = {"digest": 2, "push": 3, "sound": 4, "voice": 5}

#: Channel class → Pushover priority (-2 … 2). Capped at ``1`` (high): emergency
#: (2) requires ``retry``/``expire`` params, and ``voice`` really wants TTS/a
#: call, not a louder push — so we never send a malformed emergency alert.
PUSHOVER_PRIORITY = {"digest": -1, "push": 0, "sound": 1, "voice": 1}

#: A cue's ``context_key`` → the :mod:`~prefrontal.webhooks.notify` nudge *kind*
#: whose action buttons apply, and the ``ref`` key holding the button's target
#: id. A cue whose context isn't here simply gets no buttons (a plain push).
#: (Self-care meal/water carry a synthetic date ``target``; the tap acts on
#: "now" — see :mod:`prefrontal.modules.self_care`.)
_CONTEXT_KIND = {
    "outing": "outing",
    "departure": "departure",
    "focus": "focus",
    "meal": "meal",
    "water": "water",
}
_KIND_TARGET = {
    "outing": "outing_id",
    "departure": "commitment_id",
    "focus": "session_id",
    "meal": "target",
    "water": "target",
}


@dataclass(frozen=True)
class Route:
    """Where a given user's nudges go — resolved routing identifiers.

    Built by :func:`resolve_route` from per-user ``coaching_state`` layered over
    the operator defaults in :class:`~prefrontal.config.Settings`. Empty fields
    mean "that transport isn't configured for this user", so the router falls
    through to the next one (or no-ops).
    """

    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    pushover_token: str = ""
    pushover_user_key: str = ""
    tts_enabled: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one delivery attempt (never an exception — see module docs).

    Attributes:
        channel: The :class:`~prefrontal.coaching.Decision` channel class this
            was for (``push``/``sound``/…), stamped by the router.
        transport: Which transport handled it — ``"ntfy"``/``"pushover"``/
            ``"tts"``, or ``"none"`` when nothing was configured / it was held.
        delivered: ``True`` only if a send actually succeeded.
        status_code: HTTP status from the transport, or ``None`` (TTS / no send).
        detail: Human-readable note for logs and the CLI.
    """

    channel: str = ""
    transport: str = "none"
    delivered: bool = False
    status_code: int | None = None
    detail: str = ""


def resolve_route(store: Any, settings: Settings | None = None) -> Route:
    """Resolve a user's :class:`Route`: per-user ``coaching_state`` over operator defaults.

    Each identifier is read from the (scoped) store's ``coaching_state`` and
    falls back to the matching :class:`~prefrontal.config.Settings` field when
    unset — so an operator can configure one default target and only override
    per user for people who have their own topic/key (multi-tenant §6.5).
    """
    resolved = settings or get_settings()

    def _pref(key: str, default: str) -> str:
        value = store.get_state(key)
        return value if value else default

    return Route(
        ntfy_server=(_pref("ntfy_server", resolved.ntfy_server) or "https://ntfy.sh").rstrip("/"),
        ntfy_topic=_pref("ntfy_topic", resolved.ntfy_topic),
        ntfy_token=_pref("ntfy_token", resolved.ntfy_token),
        pushover_token=_pref("pushover_token", resolved.pushover_token),
        pushover_user_key=_pref("pushover_user_key", resolved.pushover_user_key),
        tts_enabled=store.get_bool("tts_enabled", resolved.tts_enabled),
    )


class NtfyClient:
    """Publish a notification (with inline action buttons) to an ntfy topic.

    Credential-free at construction — the topic/token ride in per publish so one
    client can serve every user's :class:`Route`. Tests inject an
    ``httpx`` transport, matching :class:`~prefrontal.integrations.ollama.OllamaClient`.
    """

    def __init__(self, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        self.timeout = timeout
        self._transport = transport

    def publish(
        self,
        server: str,
        topic: str,
        token: str = "",
        *,
        title: str,
        message: str,
        priority: int = 3,
        actions: list[dict[str, Any]] | None = None,
    ) -> DeliveryResult:
        """POST a JSON message to ``{server}/`` for ``topic``.

        Returns a no-op result (nothing sent) when ``server``/``topic`` is empty,
        so the caller can always try ntfy first and fall through when it's off.
        """
        if not server or not topic:
            return DeliveryResult(transport="ntfy", detail="ntfy: no server/topic configured")
        body: dict[str, Any] = {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
        }
        if actions:
            body["actions"] = actions
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(
                base_url=server.rstrip("/"), timeout=self.timeout, transport=self._transport
            ) as client:
                resp = client.post("/", json=body, headers=headers)
        except httpx.HTTPError as exc:  # network down, DNS, timeout, …
            return DeliveryResult(transport="ntfy", detail=f"ntfy request failed: {exc}")
        return DeliveryResult(
            transport="ntfy",
            delivered=resp.is_success,
            status_code=resp.status_code,
            detail=f"ntfy responded {resp.status_code}",
        )


class PushoverClient:
    """Publish a notification to Pushover.

    Pushover has no inline action buttons — the router passes a single tap URL as
    the message's *supplementary URL* instead, so a Pushover user still gets a
    (one-hop) way to act.
    """

    API_URL = "https://api.pushover.net/1/messages.json"

    def __init__(self, timeout: float = 10.0, transport: httpx.BaseTransport | None = None) -> None:
        self.timeout = timeout
        self._transport = transport

    def publish(
        self,
        token: str,
        user_key: str,
        *,
        title: str,
        message: str,
        priority: int = 0,
        url: str = "",
        url_title: str = "",
    ) -> DeliveryResult:
        """POST a message to the Pushover API.

        No-ops (nothing sent) unless both ``token`` and ``user_key`` are set.
        """
        if not token or not user_key:
            return DeliveryResult(
                transport="pushover", detail="pushover: no credentials configured"
            )
        data: dict[str, Any] = {
            "token": token,
            "user": user_key,
            "title": title,
            "message": message,
            "priority": priority,
        }
        if url:
            data["url"] = url
            data["url_title"] = url_title or "Open"
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                resp = client.post(self.API_URL, data=data)
        except httpx.HTTPError as exc:
            return DeliveryResult(transport="pushover", detail=f"pushover request failed: {exc}")
        return DeliveryResult(
            transport="pushover",
            delivered=resp.is_success,
            status_code=resp.status_code,
            detail=f"pushover responded {resp.status_code}",
        )


class TTSClient:
    """Speak a message aloud on the host via a local TTS command (macOS ``say``).

    The Prefrontal deployment runs on a Mac mini, so the ``voice`` channel can be
    served locally with no external account. **Off by default** (it speaks in the
    room, which is only wanted when you're at the machine); enable per user with
    the ``tts_enabled`` coaching key or ``PREFRONTAL_TTS_ENABLED``. No-ops with a
    clear detail when disabled or when the command isn't on ``PATH`` (e.g. Linux).
    """

    def __init__(self, command: tuple[str, ...] = ("say",)) -> None:
        self.command = command

    def speak(self, message: str, *, enabled: bool) -> DeliveryResult:
        if not enabled:
            return DeliveryResult(channel="voice", transport="tts", detail="tts: disabled")
        if shutil.which(self.command[0]) is None:
            return DeliveryResult(
                channel="voice", transport="tts", detail=f"tts: '{self.command[0]}' not found"
            )
        try:
            subprocess.run([*self.command, message], check=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:
            return DeliveryResult(channel="voice", transport="tts", detail=f"tts failed: {exc}")
        return DeliveryResult(
            channel="voice", transport="tts", delivered=True, detail="spoken locally"
        )


def _actions_for_cue(
    cue: Any, *, base_url: str, secret: str, handle: str
) -> list[dict[str, Any]]:
    """Build the ntfy action buttons for a cue, or ``[]`` when none apply.

    Maps the cue's ``context_key`` to a :mod:`~prefrontal.webhooks.notify` nudge
    *kind* and pulls the button's target id from ``cue.ref``; an unmapped context
    or missing target yields no buttons (``nudge_actions`` returns ``[]``), so any
    cue can be delivered as a plain push.
    """
    kind = _CONTEXT_KIND.get(cue.context_key)
    if not kind:
        return []
    ref = cue.ref or {}
    target_id = ref.get(_KIND_TARGET[kind])
    if target_id is None and kind == "departure":
        target_id = ref.get("outing_id")  # departure cues may carry the outing id
    return nudge_actions(kind, target_id, base_url=base_url, secret=secret, handle=handle)


class DeliveryClient:
    """Route a :class:`~prefrontal.coaching.Decision` to a transport and publish.

    Composes the three transports; :meth:`deliver` picks one from the channel
    class and the user's :class:`Route`, preferring **ntfy** (it can render the
    inline action buttons) and falling back to Pushover, with local TTS for
    ``voice`` when enabled. Construct with :meth:`from_settings` (tests pass an
    ``httpx`` transport that both HTTP clients share).
    """

    def __init__(
        self,
        *,
        ntfy: NtfyClient | None = None,
        pushover: PushoverClient | None = None,
        tts: TTSClient | None = None,
    ) -> None:
        self.ntfy = ntfy or NtfyClient()
        self.pushover = pushover or PushoverClient()
        self.tts = tts or TTSClient()

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None
    ) -> DeliveryClient:
        """Build a client. ``transport`` (tests) is shared by both HTTP transports."""
        return cls(
            ntfy=NtfyClient(transport=transport),
            pushover=PushoverClient(transport=transport),
            tts=TTSClient(),
        )

    def deliver(
        self,
        decision: Decision,
        route: Route,
        *,
        base_url: str = "",
        secret: str = "",
        handle: str = "",
    ) -> DeliveryResult:
        """Publish one decision on the transport its channel class maps to.

        ``digest`` is held (folds into the briefing, no send). ``voice`` speaks
        locally first when TTS is enabled, otherwise it rides the push transport
        at max priority. ``base_url``/``secret``/``handle`` sign the one-tap
        action buttons (empty → a plain push, matching ``notify``'s own guard).
        """
        channel = decision.channel
        message = decision.text
        if channel == "digest":
            return DeliveryResult(channel=channel, detail="held for digest")

        if channel == "voice" and route.tts_enabled:
            spoken = self.tts.speak(message, enabled=True)
            if spoken.delivered:
                return spoken  # already stamped channel="voice"

        actions = _actions_for_cue(decision.cue, base_url=base_url, secret=secret, handle=handle)

        if route.ntfy_topic:
            result = self.ntfy.publish(
                route.ntfy_server,
                route.ntfy_topic,
                route.ntfy_token,
                title=_TITLE,
                message=message,
                priority=NTFY_PRIORITY.get(channel, 3),
                actions=actions,
            )
            return replace(result, channel=channel)

        if route.pushover_token and route.pushover_user_key:
            url = actions[0]["url"] if actions else ""
            url_title = actions[0]["label"] if actions else ""
            result = self.pushover.publish(
                route.pushover_token,
                route.pushover_user_key,
                title=_TITLE,
                message=message,
                priority=PUSHOVER_PRIORITY.get(channel, 0),
                url=url,
                url_title=url_title,
            )
            return replace(result, channel=channel)

        return DeliveryResult(channel=channel, detail="no transport configured")

    def deliver_all(
        self,
        decisions: list[Decision],
        route: Route,
        *,
        base_url: str = "",
        secret: str = "",
        handle: str = "",
    ) -> list[DeliveryResult]:
        """Deliver every decision in order; one failure never stops the rest."""
        return [
            self.deliver(d, route, base_url=base_url, secret=secret, handle=handle)
            for d in decisions
        ]


def household_notice(message: str, *, channel: str = "push") -> Decision:
    """A minimal :class:`~prefrontal.coaching.Decision` carrying a plain household push.

    Reuses the coaching cue/decision shape so :meth:`DeliveryClient.deliver` can
    route it, but the cue's ``context_key`` is unmapped (see ``_CONTEXT_KIND``),
    so it delivers as a plain notification with no one-tap action buttons — which
    is exactly right for a "goal reached!" congratulation. ``coaching`` is
    imported lazily to keep this transport module free of the cycle its
    ``TYPE_CHECKING`` import already avoids.
    """
    from prefrontal.coaching import Cue, Decision  # lazy: avoid an import cycle

    cue = Cue(
        module="household",
        intervention="star_goal",
        urgency="nudge",
        text=message,
        context_key="household",  # unmapped → no action buttons (a plain push)
        dedup_key="household_notice",
    )
    return Decision(cue=cue, channel=channel, text=message)


def deliver_to_household(
    store: Any,
    household_id: int,
    decision: Decision,
    *,
    settings: Settings | None = None,
    client: DeliveryClient | None = None,
) -> list[dict[str, Any]]:
    """Deliver one decision to **every** member of a household (both co-parents).

    Enumerates the household's members, resolves each member's *own* :class:`Route`
    (their per-user ntfy topic / Pushover key over the operator defaults), and
    publishes to each. This is how a shared-sheet event — a reward goal reached —
    reaches both parents at once, and it is the reusable seam the v2 delta digest
    will push through too.

    Errors never raise (each :meth:`DeliveryClient.deliver` swallows transport
    failures), and a member with nothing configured yields a no-op result rather
    than being skipped — so the returned list is a faithful per-member record.

    Args:
        store: Any store that can enumerate members (``household_members``) and
            derive a per-member scoped store (``scoped``) — the app's shared store
            or a request's scoped one both work.
        household_id: The household whose members to notify.
        decision: The decision to publish (build one with :func:`household_notice`).
        settings: Operator defaults for routing (defaults to :func:`get_settings`).
        client: A :class:`DeliveryClient` (tests inject one with a mock transport).

    Returns:
        One dict per active member: ``handle``, ``display_name``, ``transport``,
        ``delivered``, ``detail``.
    """
    resolved = settings or get_settings()
    client = client or DeliveryClient.from_settings(resolved)
    out: list[dict[str, Any]] = []
    for member in store.household_members(household_id):
        if member.get("status") not in (None, "active"):
            continue
        route = resolve_route(store.scoped(member["id"]), resolved)
        result = client.deliver(decision, route, handle=member["handle"])
        out.append(
            {
                "handle": member["handle"],
                "display_name": member.get("display_name"),
                "transport": result.transport,
                "delivered": result.delivered,
                "detail": result.detail,
            }
        )
    return out
