"""Per-tag APIRouter factories for the Prefrontal webhook app.

Each module exposes ``build_router(**deps) -> APIRouter``; create_app injects
the shared services and assembles them. Route bodies are unchanged from when
they lived in create_app — only the decorator (``@app`` -> ``@router``) and
their home differ. Shared imports/models/helpers live in
:mod:`prefrontal.webhooks._common`.
"""
