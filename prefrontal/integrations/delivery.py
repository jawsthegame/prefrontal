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
On a multi-user / household box that fallback is withheld for the *targeting*
fields (see :func:`resolve_route`), so an unprovisioned user's private nudges are
never delivered to the operator's shared device.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import httpx

from prefrontal.config import Settings, get_settings
from prefrontal.log import get_logger
from prefrontal.webhooks.notify import alarm_actions_for_cue, nudge_actions

if TYPE_CHECKING:  # only a type annotation — importing it at runtime cycles via
    # coaching → scheduling → todos → integrations (this package).
    from prefrontal.coaching import Decision

logger = get_logger(__name__)

#: Notification title every channel shares, so pushes read as one assistant. The
#: leading 🧠 is a lightweight brand cue that renders on every platform (unlike
#: the ntfy ``icon``, which the iOS app ignores) — it's the strongest "this is
#: from PREFRONTAL" signal iOS actually shows on the notification.
_TITLE = "🧠 Prefrontal"

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
    "star": "star",
    "checkin": "load",
    "digest": "digest",
    "chore": "chore",
    "trip": "trip_label",
}
_KIND_TARGET = {
    "outing": "outing_id",
    "departure": "commitment_id",
    "focus": "session_id",
    "meal": "target",
    "water": "target",
    "star": "agreement_id",
    "load": "target",
    "digest": "target",
    "chore": "chore_id",
    "trip_label": "trip_id",
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
    ntfy_icon: str = ""
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

    Each identifier is read from the (scoped) store's ``coaching_state``. The
    *targeting* fields (ntfy topic/token, Pushover token/user key) fall back to
    the matching :class:`~prefrontal.config.Settings` default **only on a
    single-user box** — on a multi-user / household deployment the operator
    default is one person's device, so an unset target stays empty rather than
    delivering an unprovisioned user's private nudges to someone else (multi-tenant
    §6.5). Non-targeting fields (``ntfy_server``, ``ntfy_icon``) always default —
    they set where ntfy lives and how the push looks, not whose device it reaches.
    """
    resolved = settings or get_settings()

    # A user with no routing of their own inherits the operator's global default
    # *target* only on a SINGLE-USER box. On a multi-user / household deployment
    # that default belongs to a specific person, so inheriting it would publish
    # an unprovisioned user's PRIVATE nudges to someone else's device — a
    # cross-account leak. There, an unset target stays empty (the send no-ops)
    # until the operator gives that user their own topic/key. The non-targeting
    # fields (server, icon) still default: they say *where ntfy lives* and *what
    # the push looks like*, not *whose device it reaches*.
    multi_user = len(store.each_user(status="active")) > 1

    def _target(key: str, default: str) -> str:
        value = store.get_state(key)
        if value:
            return value
        return "" if multi_user else default

    return Route(
        ntfy_server=(
            store.get_state("ntfy_server") or resolved.ntfy_server or "https://ntfy.sh"
        ).rstrip("/"),
        ntfy_topic=_target("ntfy_topic", resolved.ntfy_topic),
        ntfy_token=_target("ntfy_token", resolved.ntfy_token),
        ntfy_icon=store.get_state("ntfy_icon") or resolved.ntfy_icon,
        pushover_token=_target("pushover_token", resolved.pushover_token),
        pushover_user_key=_target("pushover_user_key", resolved.pushover_user_key),
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
        icon: str = "",
        click: str = "",
    ) -> DeliveryResult:
        """POST a JSON message to ``{server}/`` for ``topic``.

        Returns a no-op result (nothing sent) when ``server``/``topic`` is empty,
        so the caller can always try ntfy first and fall through when it's off.

        ``icon`` (a public PNG/JPEG URL) makes the push render with the PREFRONTAL
        app icon rather than the generic ntfy glyph; ``click`` sets the default
        tap target (the dashboard), so tapping the push body opens the app. Both
        are omitted from the payload when empty.
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
        if icon:
            body["icon"] = icon
        if click:
            body["click"] = click
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(
                base_url=server.rstrip("/"), timeout=self.timeout, transport=self._transport
            ) as client:
                resp = client.post("/", json=body, headers=headers)
        except httpx.HTTPError as exc:  # network down, DNS, timeout, …
            logger.warning("ntfy delivery failed: %s", exc)
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
            logger.warning("pushover delivery failed: %s", exc)
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
            logger.warning("tts delivery failed: %s", exc)
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
    # The morning-prep nudge carries a client-side "Set alarm" view action built
    # from its own ref (no signing / server round-trip), not a signed /nudge/act
    # button, so it takes a separate path from the _CONTEXT_KIND kinds.
    if cue.context_key == "morning_prep":
        return alarm_actions_for_cue(cue)
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
        extra_actions: list[dict[str, Any]] | None = None,
    ) -> DeliveryResult:
        """Publish one decision on the transport its channel class maps to.

        ``digest`` is held (folds into the briefing, no send). ``voice`` speaks
        locally first when TTS is enabled, otherwise it rides the push transport
        at max priority. ``base_url``/``secret``/``handle`` sign the one-tap
        action buttons (empty → a plain push, matching ``notify``'s own guard).
        ``extra_actions`` overrides the context-derived buttons — for a cue whose
        buttons aren't the standard per-context set (e.g. panic's "Open triage" +
        signed "Did it"), the caller passes the exact button specs to render.
        """
        channel = decision.channel
        message = decision.text
        if channel == "digest":
            return DeliveryResult(channel=channel, detail="held for digest")

        if channel == "voice" and route.tts_enabled:
            spoken = self.tts.speak(message, enabled=True)
            if spoken.delivered:
                return spoken  # already stamped channel="voice"

        actions = (
            extra_actions
            if extra_actions is not None
            else _actions_for_cue(decision.cue, base_url=base_url, secret=secret, handle=handle)
        )

        if route.ntfy_topic:
            result = self.ntfy.publish(
                route.ntfy_server,
                route.ntfy_topic,
                route.ntfy_token,
                title=_TITLE,
                message=message,
                priority=NTFY_PRIORITY.get(channel, 3),
                actions=actions,
                # The app icon and the tap target both come from the box's own
                # public origin (``base_url``) — the same origin the phone already
                # reaches for the one-tap action buttons — so branding works for a
                # private deployment where a public GitHub URL would 404. An
                # explicit per-user/operator ``ntfy_icon`` (a hosted image) wins;
                # otherwise fall back to the box-served icon when an origin is set.
                icon=route.ntfy_icon or (f"{base_url}/brand/app-icon.png" if base_url else ""),
                # Tapping the push *body* opens the dashboard — but only for a
                # notification with no action buttons. When a nudge carries buttons,
                # a body tap would open the app and pull focus away from the one-tap
                # response (the whole reason the buttons exist), so we drop ``click``
                # and let the buttons be the only interaction. Guarded on a public
                # origin, like the buttons themselves.
                click=(
                    "" if actions else (f"{base_url}/dashboard" if base_url else "")
                ),
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


def household_prompt_notice(
    message: str, agreement_id: int, *, channel: str = "push"
) -> Decision:
    """A household push that asks whether to award a star, with one-tap Yes / Not-today.

    Unlike :func:`household_notice`, this carries ``context_key="star"`` and the
    chart's ``agreement_id`` in ``ref`` so :meth:`DeliveryClient.deliver` attaches
    the signed ⭐ Yes / Not today buttons (built per recipient in ``notify.py``) —
    tapping ⭐ Yes hits ``/nudge/act`` and awards a star with no app switch.
    """
    from prefrontal.coaching import Cue, Decision  # lazy: avoid an import cycle

    cue = Cue(
        module="household",
        intervention="star_prompt",
        urgency="nudge",
        text=message,
        context_key="star",
        dedup_key=f"star_prompt:{agreement_id}",
        ref={"agreement_id": agreement_id},
    )
    return Decision(cue=cue, channel=channel, text=message)


def household_checkin_notice(message: str, *, channel: str = "push") -> Decision:
    """A weekly mental-load check-in push, with one-tap self-report buttons.

    Carries ``context_key="checkin"`` so :meth:`DeliveryClient.deliver` attaches
    the signed *Felt light / Balanced / Carried a lot* buttons (built per recipient
    in ``notify.py``). The check-in has no entity id — it acts on "this week" — so
    it rides a synthetic ``target`` like the self-care checks; the tap resolves the
    week at ``/nudge/act`` time.
    """
    from prefrontal.coaching import Cue, Decision  # lazy: avoid an import cycle

    cue = Cue(
        module="household",
        intervention="load_checkin",
        urgency="nudge",
        text=message,
        context_key="checkin",
        dedup_key="load_checkin",
        ref={"target": 0},
    )
    return Decision(cue=cue, channel=channel, text=message)


def household_digest_notice(message: str, *, channel: str = "push") -> Decision:
    """A daily delta-digest push with a one-tap "Caught up" button.

    Carries ``context_key="digest"`` so :meth:`DeliveryClient.deliver` attaches the
    signed *Caught up 👍* button (``notify.py``); tapping it marks the sheet seen
    at ``/nudge/act`` so the parent isn't re-nudged about the same changes.
    """
    from prefrontal.coaching import Cue, Decision  # lazy: avoid an import cycle

    cue = Cue(
        module="household",
        intervention="delta_digest",
        urgency="ambient",
        text=message,
        context_key="digest",
        dedup_key="household_digest",
        ref={"target": 0},
    )
    return Decision(cue=cue, channel=channel, text=message)


def household_chore_notice(
    message: str, chore_id: int, *, channel: str = "push"
) -> Decision:
    """A shared-chore push (reminder or miss-handoff) with a one-tap "Done" button.

    Carries ``context_key="chore"`` and the chore's id in ``ref`` so
    :meth:`DeliveryClient.deliver` attaches the signed ✓ Done button (built per
    recipient in ``notify.py``) — tapping it marks the chore done for today with no
    app switch, attributed to whoever tapped. The same builder serves the owner's
    reminder, the owner's still-not-done nudge, and the other parent's heads-up;
    only the ``message`` differs (see :mod:`prefrontal.household`).
    """
    from prefrontal.coaching import Cue, Decision  # lazy: avoid an import cycle

    cue = Cue(
        module="household",
        intervention="chore_reminder",
        urgency="nudge",
        text=message,
        context_key="chore",
        dedup_key=f"chore:{chore_id}",
        ref={"chore_id": chore_id},
    )
    return Decision(cue=cue, channel=channel, text=message)


def deliver_to_member(
    store: Any,
    decision: Decision,
    *,
    handle: str,
    settings: Settings | None = None,
    client: DeliveryClient | None = None,
    base_url: str = "",
    secret: str = "",
) -> dict[str, Any]:
    """Deliver one decision to a single member on their own route (not the whole household).

    ``store`` must be scoped to that member (so :func:`resolve_route` reads their
    routing). Used by the delta digest, which is personalized per parent rather
    than fanned to everyone. Never raises — returns a per-member outcome dict.
    """
    resolved = settings or get_settings()
    client = client or DeliveryClient.from_settings(resolved)
    route = resolve_route(store, resolved)
    result = client.deliver(decision, route, base_url=base_url, secret=secret, handle=handle)
    return {
        "handle": handle,
        "transport": result.transport,
        "delivered": result.delivered,
        "detail": result.detail,
    }


def deliver_to_household(
    store: Any,
    household_id: int,
    decision: Decision,
    *,
    settings: Settings | None = None,
    client: DeliveryClient | None = None,
    base_url: str = "",
    secret: str = "",
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
        base_url: Public origin for signing one-tap buttons (empty → plain push).
        secret: Signing key for one-tap buttons (empty → plain push).

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
        # Buttons are signed per recipient (each member's own handle), so ⭐ Yes
        # attributes the award to whoever taps it.
        result = client.deliver(
            decision, route, base_url=base_url, secret=secret, handle=member["handle"]
        )
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
