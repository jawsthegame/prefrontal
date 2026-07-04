"""Registry of challenge-area modules.

Modules register themselves here on import (see each module file's bottom and
``prefrontal/modules/__init__.py``, which imports the built-ins). The registry
is the single place the rest of the system asks "which modules exist?" and
"which are enabled for this configuration?".
"""

from __future__ import annotations

from prefrontal.config import Settings, get_settings
from prefrontal.modules.base import Module

#: Insertion-ordered map of module key -> module instance.
_REGISTRY: dict[str, Module] = {}


def register(module: Module) -> Module:
    """Add a module to the registry.

    Args:
        module: The module instance to register. Its ``key`` must be set and
            unique.

    Returns:
        The same module, so this can be used inline at module scope.

    Raises:
        ValueError: If the module has no key or the key is already registered.
    """
    if not module.key:
        raise ValueError(f"Module {module!r} has no key.")
    if module.key in _REGISTRY:
        raise ValueError(f"Module key already registered: {module.key!r}")
    _REGISTRY[module.key] = module
    return module


def available() -> list[Module]:
    """Return all registered modules in registration order."""
    return list(_REGISTRY.values())


def get(key: str) -> Module:
    """Return a single module by key.

    Args:
        key: The module key.

    Returns:
        The registered :class:`~prefrontal.modules.base.Module`.

    Raises:
        KeyError: If no module with that key is registered.
    """
    return _REGISTRY[key]


def enabled_modules(settings: Settings | None = None) -> list[Module]:
    """Return the modules enabled for the given settings.

    An empty ``settings.modules`` means "enable everything" (the default for a
    fresh install). Otherwise only the listed keys are enabled, in the order
    they appear in the configuration. Unknown keys are ignored so a typo or a
    removed module never crashes startup.

    Args:
        settings: Settings to read the module list from. Defaults to
            :func:`prefrontal.config.get_settings`.

    Returns:
        The enabled module instances.
    """
    resolved = settings or get_settings()
    if resolved.all_modules_enabled:
        return available()
    # A specific module list is also *extended* by any modules an enabled Context
    # Pack switches on (e.g. the Parent pack turns on time_blindness). Lazy import
    # keeps the modules layer free of a hard dependency on the packs package.
    from prefrontal.packs.registry import pack_module_keys

    keys: list[str] = list(resolved.modules)
    for key in pack_module_keys(resolved):
        if key not in keys:
            keys.append(key)
    return [_REGISTRY[k] for k in keys if k in _REGISTRY]


def is_enabled(key: str, settings: Settings | None = None) -> bool:
    """Return whether the module ``key`` is enabled for the given settings.

    A single-key convenience over :func:`enabled_modules`, used by the
    intervention entry points (the webhook "check" routes) to suppress a
    disabled module's proactive nudges — so disabling a module actually turns off
    its behavior, not just its profile section.

    An empty ``settings.modules`` enables every registered module (the
    fresh-install default). An unknown or unregistered key is treated as disabled.

    Args:
        key: The module key to test.
        settings: Settings to read the module list from. Defaults to
            :func:`prefrontal.config.get_settings`.

    Returns:
        ``True`` if the module is registered and enabled.
    """
    resolved = settings or get_settings()
    if key not in _REGISTRY:
        return False
    if resolved.all_modules_enabled or key in resolved.modules:
        return True
    # Also enabled if a Context Pack switched it on (lazy import — see
    # :func:`enabled_modules`).
    from prefrontal.packs.registry import pack_module_keys

    return key in pack_module_keys(resolved)
