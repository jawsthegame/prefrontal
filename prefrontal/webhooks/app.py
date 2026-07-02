"""FastAPI application factory for the Prefrontal webhook service.

The routes live in per-tag modules under :mod:`prefrontal.webhooks.routers`;
this module builds the shared services, injects them into each router, and
assembles the app. Shared imports, Pydantic models, constants, and request
dependencies live in :mod:`prefrontal.webhooks._common`.
"""
from __future__ import annotations

from prefrontal.webhooks._common import (
    APP_VERSION,
    INFER_TIMEOUT_SECONDS,
    AnthropicClient,
    FastAPI,
    MemoryStore,
    N8nClient,
    NominatimGeocoder,
    OllamaClient,
    Settings,
    asynccontextmanager,
    enrich_commitments,
    get_settings,
    register_oauth_routes,
)


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
    # Optional Claude client for the dashboard assistant. Local-first: only used
    # when an API key is configured (``available()``), otherwise the assistant
    # falls back to the local Ollama client.
    anthropic_client = anthropic or AnthropicClient.from_settings(resolved_settings)
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
        try:
            yield
        finally:
            if owns_store:
                active_store.close()

    app = FastAPI(
        title="Prefrontal Webhooks",
        version=APP_VERSION,
        summary="Low-friction ingestion for iOS Shortcut and n8n triggers.",
        lifespan=lifespan,
    )

    # Google sign-in for the web surfaces (browser session cookie); the machine
    # clients keep using per-user tokens. resolve_user accepts either.
    register_oauth_routes(app, resolved_settings)

    from prefrontal.webhooks.routers import (
        admin,
        anchor,
        assistant,
        coaching,
        focus,
        impulsivity,
        ingestion,
        memory,
        schedule,
        system,
        todos,
    )
    _router_deps = dict(
        resolved_settings=resolved_settings,
        n8n=n8n,
        ollama_client=ollama_client,
        summarizer_client=summarizer_client,
        geocoder_client=geocoder_client,
        _run_geocode=_run_geocode,
    )
    for _module in (
        system,
        memory,
        ingestion,
        anchor,
        impulsivity,
        focus,
        schedule,
        todos,
        coaching,
        admin,
    ):
        app.include_router(_module.build_router(**_router_deps))
    # The assistant router needs one extra service (the Claude client), so it's
    # included separately rather than through the shared-deps loop above.
    app.include_router(
        assistant.build_router(**_router_deps, anthropic_client=anthropic_client)
    )
    return app


#: Module-level application instance imported by prefrontal.webhooks and used
#: as the ASGI target for `uvicorn prefrontal.webhooks.app:app`.
app = create_app()
