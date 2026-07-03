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

_configured = False


def configure_logging(*, level: str | None = None) -> None:
    """Install a root log handler and set the level, once (idempotent).

    Args:
        level: Explicit level name (e.g. ``"DEBUG"``). Defaults to
            ``PREFRONTAL_LOG_LEVEL`` in the environment, then ``"INFO"``.
    """
    global _configured
    if _configured:
        return
    name = (level or os.environ.get("PREFRONTAL_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger. Library code should pass ``__name__``."""
    return logging.getLogger(name)
