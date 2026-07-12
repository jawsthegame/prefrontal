"""Logging seam for Prefrontal.

A single place to obtain a logger and to configure handlers once at process
startup, so a swallowed failure in the field (a broken module evaluator, a
notification that never went out) leaves a trace instead of vanishing.

The contract is the standard library one:

- **Library code** calls :func:`get_logger` (``get_logger(__name__)``) and logs.
  It never configures handlers or sets levels — that would fight the host app.
- **Entry points** (the webhook app, the CLI) call :func:`configure_logging`
  exactly once at startup to install a handler and pick the level.

Level defaults to ``INFO`` and can be overridden with ``PREFRONTAL_LOG_LEVEL``
(e.g. ``DEBUG``, ``WARNING``). Configuration is idempotent, so it is safe for
more than one entry point to call it.
"""

from __future__ import annotations

import logging
import os

#: The logger namespace Prefrontal configures. Every :func:`get_logger`
#: (``get_logger(__name__)``) returns a ``prefrontal.*`` descendant of it.
_NAMESPACE = "prefrontal"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_configured = False


def configure_logging(*, level: str | None = None) -> None:
    """Install a handler on Prefrontal's logger namespace and set its level, once.

    Configures the ``prefrontal`` logger (parent of every ``get_logger(__name__)``)
    rather than calling :func:`logging.basicConfig` on the root logger.
    ``basicConfig`` is a **no-op when the root logger already has handlers** — which
    is exactly the case under uvicorn/gunicorn, so the deployed webhook server never
    got Prefrontal's intended format. Attaching the handler to our own namespace and
    stopping propagation gives Prefrontal's records a consistent format regardless of
    the host, and avoids double-logging them through the host's root handlers.

    Idempotent (guarded by ``_configured``) so multiple entry points can call it
    without stacking duplicate handlers.

    Args:
        level: Explicit level name (e.g. ``"DEBUG"``). Defaults to
            ``PREFRONTAL_LOG_LEVEL`` in the environment, then ``"INFO"``.
    """
    global _configured
    if _configured:
        return
    name = (level or os.environ.get("PREFRONTAL_LOG_LEVEL") or "INFO").upper()
    logger = logging.getLogger(_NAMESPACE)
    logger.setLevel(getattr(logging, name, logging.INFO))
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False  # our handler is authoritative; don't also hit root
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger. Library code should pass ``__name__``."""
    return logging.getLogger(name)
