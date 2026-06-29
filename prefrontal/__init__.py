"""Prefrontal — an open source executive function agent system.

Prefrontal is a self-hosted assistant that learns from your behavior to help you
manage time, attention, and task switching. The package is organized into small,
focused subpackages mirroring the architecture described in the project README:

- :mod:`prefrontal.memory` — the SQLite behavioral memory layer (the core).
- :mod:`prefrontal.webhooks` — the FastAPI listener for iOS Shortcut triggers.
- :mod:`prefrontal.integrations` — outbound/inbound integrations such as n8n.
- :mod:`prefrontal.config` — environment-driven runtime settings.
- :mod:`prefrontal.cli` — the ``prefrontal`` command line entry point.

See ``docs/schema.md`` for the memory schema and ``CONTRIBUTING.md`` for layout
and conventions.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
