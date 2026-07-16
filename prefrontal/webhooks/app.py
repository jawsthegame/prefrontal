"""FastAPI application factory for the Prefrontal webhook service.

The routes live in per-tag modules under :mod:`prefrontal.webhooks.routers`;
this module builds the shared services, injects them into each router, and
assembles the app. Request/response models live in
:mod:`prefrontal.webhooks.schemas`, request dependencies in
:mod:`prefrontal.webhooks.deps`, and shared helpers in
:mod:`prefrontal.webhooks.helpers`.
"""
from __future__ import annotations

from contextlib import (
    asynccontextmanager,
)

from fastapi import (
    FastAPI,
)

from prefrontal.config import (
    Settings,
    get_settings,
)
from prefrontal.geocode import (
    enrich_commitments,
)
from prefrontal.integrations import ProviderResolver
from prefrontal.integrations.anthropic import (
    AnthropicClient,
)
from prefrontal.integrations.n8n import (
    N8nClient,
)
from prefrontal.integrations.nominatim import (
    NominatimGeocoder,
)
from prefrontal.integrations.ollama import (
    OllamaClient,
)
from prefrontal.log import configure_logging, get_logger
from prefrontal.memory.store import (
    MemoryStore,
)
from prefrontal.webhooks._common import (
    APP_VERSION,
    INFER_TIMEOUT_SECONDS,
)
from prefrontal.webhooks.oauth import (
    register_oauth_routes,
)
from prefrontal.webhooks.services import RouterServices


def create_app(
    *,
    store: MemoryStore | None = None,
    settings: Settings | None = None,
    ollama: OllamaClient | None = None,
    anthropic: AnthropicClient | None = None,
    geocoder: NominatimGeocoder | None = None,
) -> FastAPI:
    """Build and return the Prefrontal webhook application.

    Args:
        store: An optional pre-built :class:`MemoryStore`. When provided (e.g. by
            tests with an in-memory database) it is used as-is and its lifecycle
            is the caller's responsibility. When omitted, the app opens and
            initializes a store from ``settings.db_path`` on startup and closes
            it on shutdown.
        settings: Optional settings override. Defaults to :func:`get_settings`.

    Returns:
        A configured :class:`fastapi.FastAPI` instance.
    """
    configure_logging()
    resolved_settings = settings or get_settings()
    n8n = N8nClient.from_settings(resolved_settings)
    # Client used to infer a window when a start states none. Built from settings
    # unless injected (tests pass a mock-transport client to stay offline).
    ollama_client = ollama or OllamaClient(
        base_url=resolved_settings.ollama_url,
        model=resolved_settings.ollama_model,
        timeout=INFER_TIMEOUT_SECONDS,
    )
    # Separate client for the on-demand ``GET /profile?refresh=1`` summary, with
    # the full (longer) generation timeout — summarizing the whole profile is a
    # heavier call than the snappy window inference above. Reuses an injected
    # client in tests so the refresh path stays offline.
    summarizer_client = ollama or OllamaClient.from_settings(resolved_settings)
    # Optional Claude client. Local-first: only used when an API key is
    # configured (``available()``), otherwise agents fall back to Ollama.
    anthropic_client = anthropic or AnthropicClient.from_settings(resolved_settings)
    # Per-agent backend selector: which agents prefer Claude (``ANTHROPIC_AGENTS``)
    # vs the local model. Built once here; each router asks it for its agent's
    # client. Defaults to the snappy inference client as the local fallback —
    # the summarizer path passes its longer-timeout client explicitly.
    provider = ProviderResolver(
        ollama=ollama_client,
        anthropic=anthropic_client,
        anthropic_agents=frozenset(resolved_settings.anthropic_agents),
    )
    # Surface operator typos: an ANTHROPIC_AGENTS entry that matches no real agent
    # is silently ignored, so warn once at startup rather than leaving a mistyped
    # name a mystery ("why isn't the assistant using Claude?").
    unknown_agents = provider.unknown_agents()
    if unknown_agents:
        get_logger(__name__).warning(
            "ANTHROPIC_AGENTS contains unknown agent name(s): %s — they match no real "
            "agent and are ignored. Check the spelling against the known agents.",
            ", ".join(sorted(unknown_agents)),
        )
    # Surface an inert focus-balance guardrail: a Context Pack that seeds the
    # feature's config (weekly focus_target:* / the focus_balance_nudge flag) while
    # the module that actually runs it (trip_tracking) is disabled. Left unwarned,
    # this silently produces "no trips tracked" and an empty/one-sphere balance —
    # the config is seeded but nothing populates or nudges it. Warn once so the
    # cause isn't a mystery (this is the shape of a bug the built-in packs once
    # shipped with; caught here if a module list or custom pack reintroduces it).
    from prefrontal.packs import focus_balance_seeding_gap

    balance_gap_packs = focus_balance_seeding_gap(resolved_settings)
    if balance_gap_packs:
        get_logger(__name__).warning(
            "Context pack(s) %s seed the focus-balance guardrail (focus_target/"
            "focus_balance_nudge) but the trip_tracking module is disabled — the "
            "weekly targets and nudge will be inert and no closed-loop trips will be "
            "tracked. Enable trip_tracking (add it to PREFRONTAL_MODULES, or leave "
            "the list blank to enable all modules).",
            ", ".join(sorted(balance_gap_packs)),
        )
    # Forward-geocoder for commitment destinations. Built from settings unless
    # injected (tests pass a stub). Only consulted when the runtime
    # ``geocoding_enabled`` flag is on — see ``_run_geocode`` below.
    geocoder_client = geocoder or NominatimGeocoder.from_settings(resolved_settings)

    def _run_geocode(memory: MemoryStore, *, limit: int = 25) -> dict[str, int]:
        """Enrich commitments with destination coords (best-effort).

        Curated places and the cache are always consulted (offline); the network
        geocoder is passed only when the ``geocoding_enabled`` coaching-state flag
        is on, so a fresh install never reaches off-host by default.
        """
        enabled = memory.get_bool("geocoding_enabled", False)
        return enrich_commitments(
            memory, geocoder=geocoder_client if enabled else None, limit=limit
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Open the memory store on startup; close it on shutdown if we own it."""
        owns_store = store is None
        # Per-thread connections: FastAPI runs the sync endpoints in a
        # threadpool, and a single shared connection is not safe across threads
        # (it interleaves statements and returns garbled result sets).
        active_store = store or MemoryStore.threaded(resolved_settings.db_path)
        app.state.store = active_store
        app.state.settings = resolved_settings
        app.state.n8n = n8n
        # Delegation prep can be slow (a full-transcript model call), so the router
        # runs it on a background thread — but only when we own the *threaded* store
        # (each thread gets its own connection). A caller-injected store (tests) is
        # single-connection and unsafe across threads, so prep runs inline there.
        app.state.delegation_async = owns_store
        try:
            yield
        finally:
            if owns_store:
                active_store.close()

    app = FastAPI(
        title="Prefrontal Webhooks",
        version=APP_VERSION,
        summary="Low-friction ingestion for native-app, Shortcut, and n8n triggers.",
        lifespan=lifespan,
    )

    # Google sign-in for the web surfaces (browser session cookie); the machine
    # clients keep using per-user tokens. resolve_user accepts either.
    register_oauth_routes(app, resolved_settings)

    # Observe which pull surfaces (dashboard/panic/briefing/…) actually get
    # opened — the `invoked` half of the feature-usage loop. Pure side-effect
    # middleware: it never alters the response.
    from prefrontal.webhooks.usage import usage_middleware

    app.middleware("http")(usage_middleware)

    from prefrontal.webhooks.routers import (
        actions,
        admin,
        anchor,
        assistant,
        blockers,
        clarify,
        coaching,
        communicate,
        focus,
        household,
        impulsivity,
        ingestion,
        memory,
        packs,
        people,
        projects,
        schedule,
        search,
        sensor,
        system,
        todos,
    )
    services = RouterServices(
        settings=resolved_settings,
        n8n=n8n,
        ollama=ollama_client,
        summarizer=summarizer_client,
        anthropic=anthropic_client,
        provider=provider,
        geocoder=geocoder_client,
        run_geocode=_run_geocode,
    )
    # Every router takes the same bundle and binds only what it uses — including
    # the assistant, whose Claude client is just another field now, so there is
    # no longer a special case here.
    for _module in (
        system,
        memory,
        ingestion,
        anchor,
        impulsivity,
        focus,
        schedule,
        todos,
        projects,
        blockers,
        search,
        coaching,
        communicate,
        actions,
        household,
        sensor,
        clarify,
        people,
        admin,
        assistant,
        packs,
    ):
        app.include_router(_module.build_router(services))
    return app


#: Module-level application instance imported by prefrontal.webhooks and used
#: as the ASGI target for `uvicorn prefrontal.webhooks.app:app`.
app = create_app()
