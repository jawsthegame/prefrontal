"""The webhook listener — Prefrontal's low-friction ingestion surface.

A FastAPI application (see :mod:`prefrontal.webhooks.app`) receives triggers from
iOS Shortcuts and n8n and turns them into rows in the behavioral memory layer.
The design goal is one-tap capture: logging an outcome must be easier than
ignoring it, or the system fails.
"""

from prefrontal.webhooks.app import app, create_app

__all__ = ["app", "create_app"]
