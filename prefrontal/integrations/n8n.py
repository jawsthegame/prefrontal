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

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from prefrontal.config import Settings, get_settings
from prefrontal.log import get_logger

logger = get_logger(__name__)

#: Repo-relative directory holding the importable workflow templates that the
#: workflow sync pushes into the running n8n. Matches ``deploy/README.md``.
DEFAULT_WORKFLOW_DIR = "deploy/n8n"

#: Top-level workflow keys n8n's Public REST API accepts on create/update. The
#: API rejects any other property ("request/body must NOT have additional
#: properties"), and ``active``/``id``/``tags`` are managed through dedicated
#: endpoints — so every other key (``active``, ``id``, ``meta``, ``pinData``,
#: ``tags``, ``versionId``, …) is stripped before sending.
_WRITABLE_KEYS = ("name", "nodes", "connections", "settings", "staticData")


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


@dataclass(frozen=True)
class WorkflowSync:
    """Outcome of pushing one workflow template into n8n.

    Attributes:
        name: The workflow's ``name`` (the upsert key — templates all carry a
            unique ``"Prefrontal — …"`` name).
        action: What happened — ``"created"``, ``"updated"``, or ``"failed"``.
        ok: ``True`` if the workflow reached its intended definition *and*
            active state; ``False`` on any transport/validation error.
        workflow_id: The n8n workflow id after the upsert (``None`` if unknown).
        active: The resulting active state (``True``/``False``), or ``None`` if
            active state wasn't touched (``activate=False`` or the upsert failed
            before it got that far).
        detail: Human-readable note (status code, or the error).
    """

    name: str
    action: str
    ok: bool = True
    workflow_id: str | None = None
    active: bool | None = None
    detail: str = ""


class N8nWorkflowSyncer:
    """Pushes ``deploy/n8n/*.json`` templates into a running n8n via its REST API.

    This is the "update n8n directly" half of the integration: an update (the
    dashboard **Update** button / ``prefrontal update`` → ``deploy/update.sh``)
    calls ``prefrontal n8n push``, which upserts every workflow template into the
    live n8n so a repo change becomes live workflows without a manual editor
    import.

    Design mirrors :class:`N8nClient`'s local-first stance:

    * **Skip if unconfigured.** With no ``api_url``/``api_key`` the syncer is
      disabled and :meth:`push` is a clean no-op — nothing leaves the box and an
      update never fails for lack of n8n config.
    * **Idempotent upsert by name.** Workflows are matched to existing ones by
      ``name`` (``PUT`` the match, else ``POST`` a new one), so re-running an
      update updates in place rather than piling up duplicates. No template
      needs a pinned id.
    * **Declarative activation.** Each template's own ``active`` flag is
      authoritative: after the upsert the syncer converges n8n to it
      (``/activate`` vs ``/deactivate``). The shipped templates are all
      ``active: false``, so a sync never surprise-enables an opt-in workflow —
      flip ``"active": true`` in a file to have that workflow auto-activate on
      the next update. Pass ``activate=False`` to leave active state untouched.

    What the sync deliberately does **not** own: n8n *credentials* (the API never
    exports credential secrets, so the ``Prefrontal Token``/ntfy/Twilio creds
    stay a one-time manual setup, referenced by name), and node ``typeVersion``
    drift across n8n releases. See ``docs/n8n-sync.md``.

    Args:
        api_url: Base URL of n8n's Public REST API (``.../api/v1``). Empty
            disables the syncer.
        api_key: API key sent as ``X-N8N-API-KEY``. Empty disables the syncer.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self, api_url: str = "", api_key: str = "", timeout: float = 30.0
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> N8nWorkflowSyncer:
        """Build a syncer from :class:`~prefrontal.config.Settings`."""
        resolved = settings or get_settings()
        return cls(api_url=resolved.n8n_api_url, api_key=resolved.n8n_api_key)

    @property
    def enabled(self) -> bool:
        """Whether both an API URL and key are configured (else :meth:`push` no-ops)."""
        return bool(self.api_url and self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "X-N8N-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _payload(doc: dict[str, Any]) -> dict[str, Any]:
        """Reduce a workflow template to the fields the REST API accepts."""
        payload = {key: doc[key] for key in _WRITABLE_KEYS if key in doc}
        # n8n requires a `settings` object on create; default it if the template omits one.
        payload.setdefault("settings", {})
        return payload

    def _existing_ids(self, client: httpx.Client) -> dict[str, str]:
        """Map existing workflow ``name`` → ``id``, following pagination."""
        mapping: dict[str, str] = {}
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = client.get(
                f"{self.api_url}/workflows",
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            for wf in body.get("data", []):
                name, wid = wf.get("name"), wf.get("id")
                if name and wid is not None:
                    mapping[name] = str(wid)
            cursor = body.get("nextCursor")
            if not cursor:
                return mapping

    def _set_active(self, client: httpx.Client, workflow_id: str, want: bool) -> bool:
        """Converge a workflow's active state to ``want``; returns ``want`` on success."""
        verb = "activate" if want else "deactivate"
        resp = client.post(
            f"{self.api_url}/workflows/{workflow_id}/{verb}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return want

    def _push_one(
        self,
        client: httpx.Client,
        path: Path,
        existing: dict[str, str],
        activate: bool,
    ) -> WorkflowSync:
        """Upsert one template file, then (optionally) converge its active state."""
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return WorkflowSync(name=path.name, action="failed", ok=False,
                                detail=f"could not read {path.name}: {exc}")
        name = str(doc.get("name") or path.stem)
        payload = self._payload(doc)
        workflow_id = existing.get(name)
        try:
            if workflow_id:
                resp = client.put(
                    f"{self.api_url}/workflows/{workflow_id}",
                    json=payload, headers=self._headers(), timeout=self.timeout,
                )
                action = "updated"
            else:
                resp = client.post(
                    f"{self.api_url}/workflows",
                    json=payload, headers=self._headers(), timeout=self.timeout,
                )
                action = "created"
            resp.raise_for_status()
            workflow_id = str(resp.json().get("id") or workflow_id or "")
            active: bool | None = None
            if activate and workflow_id:
                active = self._set_active(client, workflow_id, bool(doc.get("active", False)))
            return WorkflowSync(name=name, action=action, ok=True,
                                workflow_id=workflow_id or None, active=active,
                                detail=f"{action} ({resp.status_code})")
        except httpx.HTTPError as exc:
            return WorkflowSync(name=name, action="failed", ok=False,
                                workflow_id=workflow_id,
                                detail=f"{type(exc).__name__}: {exc}")

    def push(
        self,
        workflow_dir: str = DEFAULT_WORKFLOW_DIR,
        *,
        activate: bool = True,
        client: httpx.Client | None = None,
    ) -> dict[str, Any]:
        """Upsert every ``*.json`` in ``workflow_dir`` into n8n.

        Args:
            workflow_dir: Directory of workflow templates (default ``deploy/n8n``).
            activate: When ``True`` (default), converge each workflow's active
                state to its template's ``active`` flag. ``False`` upserts the
                definition only and never toggles active state.
            client: Optional pre-built :class:`httpx.Client` (tests inject a
                mock transport); one is created and closed otherwise.

        Returns:
            A JSON-able report: ``enabled`` (was the syncer configured),
            ``ok`` (did every workflow sync cleanly), ``pushed`` (a list of
            per-workflow :class:`WorkflowSync` dicts), and ``detail``. When
            disabled it returns immediately with ``enabled=False`` and
            ``ok=True`` — a skip is a success, so an update never fails for lack
            of n8n config.
        """
        if not self.enabled:
            return {"enabled": False, "ok": True, "pushed": [],
                    "detail": "n8n API not configured; skipped workflow sync"}
        files = sorted(Path(workflow_dir).glob("*.json"))
        if not files:
            return {"enabled": True, "ok": True, "pushed": [],
                    "detail": f"no workflow templates found in {workflow_dir}"}
        owns_client = client is None
        client = client or httpx.Client(timeout=self.timeout)
        try:
            try:
                existing = self._existing_ids(client)
            except httpx.HTTPError as exc:
                return {"enabled": True, "ok": False, "pushed": [],
                        "detail": f"could not list workflows from n8n: {exc}"}
            results = [self._push_one(client, path, existing, activate) for path in files]
        finally:
            if owns_client:
                client.close()
        ok = all(r.ok for r in results)
        failed = [r.name for r in results if not r.ok]
        detail = (f"synced {len(results)} workflow(s)" if ok
                  else f"{len(failed)} of {len(results)} workflow(s) failed: {', '.join(failed)}")
        return {"enabled": True, "ok": ok,
                "pushed": [asdict(r) for r in results], "detail": detail}
