"""Registry of Context Packs.

Packs register themselves here on import (see each pack file's bottom and
:mod:`prefrontal.packs`, which imports the built-ins). This is the single place
the rest of the system asks "which packs exist?" and "which are enabled?".

Unlike modules — where an empty ``PREFRONTAL_MODULES`` means *enable everything*
(a fresh install wants every challenge covered) — packs default to **none**: a
life-context pack is an explicit opt-in ("I'm managing kids"), so an unset
``PREFRONTAL_PACKS`` enables no pack and the system behaves exactly as before.
"""

from __future__ import annotations

from typing import Any

from prefrontal.config import Settings, get_settings
from prefrontal.packs.base import Pack, PackVocabulary, SituationTool

#: Insertion-ordered map of pack key -> pack instance.
_REGISTRY: dict[str, Pack] = {}


def register(pack: Pack) -> Pack:
    """Add a pack to the registry.

    Args:
        pack: The pack instance to register. Its ``key`` must be set and unique.

    Returns:
        The same pack, so this can be used inline at module scope.

    Raises:
        ValueError: If the pack has no key or the key is already registered.
    """
    if not pack.key:
        raise ValueError(f"Pack {pack!r} has no key.")
    if pack.key in _REGISTRY:
        raise ValueError(f"Pack key already registered: {pack.key!r}")
    _REGISTRY[pack.key] = pack
    return pack


def available() -> list[Pack]:
    """Return all registered packs in registration order."""
    return list(_REGISTRY.values())


def get(key: str) -> Pack:
    """Return a single pack by key.

    Raises:
        KeyError: If no pack with that key is registered.
    """
    return _REGISTRY[key]


def enabled_packs(settings: Settings | None = None) -> list[Pack]:
    """Return the packs enabled for the given settings, in configured order.

    Only the keys listed in ``PREFRONTAL_PACKS`` are enabled (none by default),
    in the order they appear — the order that determines vocabulary precedence.
    Unknown keys are ignored so a typo or a removed pack never crashes startup.

    Args:
        settings: Settings to read the pack list from. Defaults to
            :func:`prefrontal.config.get_settings`.
    """
    resolved = settings or get_settings()
    return [_REGISTRY[k] for k in resolved.packs if k in _REGISTRY]


def is_enabled(key: str, settings: Settings | None = None) -> bool:
    """Return whether the pack ``key`` is registered and enabled."""
    resolved = settings or get_settings()
    return key in _REGISTRY and key in resolved.packs


#: Coaching-state key prefix for the per-user pack on/off overlay (the Settings
#: "Features" toggles). A value of ``"off"`` hides that pack's user-facing
#: surfaces — its situation tools and the ``/care`` lens — for this user only.
#: This is the **surfaces** overlay (P1): a pack's vocabulary and domain
#: classification stay deployment-wide, so "off" is cosmetic, not structural.
PACK_ENABLED_PREFIX = "pack_enabled:"


def user_disabled_pack_keys(store: Any) -> set[str]:
    """Pack keys the signed-in user has turned **off** for themselves.

    The per-user pack overlay — the pack twin of the module overlay. Scans every
    registered pack's override so an operator enabling a pack later still honors a
    prior user "off". Best-effort: a store without the state repo reports none
    disabled, so the overlay can never hard-fail a surface.
    """
    out: set[str] = set()
    try:
        for pack in available():
            if (store.get_state(f"{PACK_ENABLED_PREFIX}{pack.key}") or "").strip().lower() == "off":
                out.add(pack.key)
    except Exception:  # noqa: BLE001 — the overlay is a convenience, never a hard gate
        return set()
    return out


def user_pack_enabled(store: Any, key: str, settings: Settings | None = None) -> bool:
    """Deployment-enabled **and** not turned off by this user (surfaces P1)."""
    return is_enabled(key, settings) and key not in user_disabled_pack_keys(store)


def user_enabled_packs(store: Any, settings: Settings | None = None) -> list[Pack]:
    """Deployment-enabled packs minus the ones this user turned off."""
    off = user_disabled_pack_keys(store)
    return [p for p in enabled_packs(settings) if p.key not in off]


def user_enabled_situations(
    store: Any, settings: Settings | None = None
) -> list[SituationTool]:
    """Situation tools from the packs this user hasn't turned off (per-user P1).

    The per-user counterpart to :func:`enabled_situations`: a tool whose owning
    pack the user disabled disappears from their ``/packs/situations`` list, just
    as a deployment-disabled pack's tools never appear at all.
    """
    out: list[SituationTool] = []
    seen: set[str] = set()
    for pack in user_enabled_packs(store, settings):
        for tool in pack.situations:
            if tool.key not in seen:
                seen.add(tool.key)
                out.append(tool)
    return out


def user_get_situation(
    store: Any, key: str, settings: Settings | None = None
) -> SituationTool | None:
    """An enabled situation tool by key, respecting the user's pack overlay.

    ``None`` covers "no such tool", "its pack is off deployment-wide", and "the
    user turned its pack off" — the router turns any of them into a 404.
    """
    for tool in user_enabled_situations(store, settings):
        if tool.key == key:
            return tool
    return None


def pack_module_keys(settings: Settings | None = None) -> list[str]:
    """Challenge-module keys switched on by the enabled packs (first-seen order).

    Consumed by :func:`prefrontal.modules.registry.enabled_modules` /
    ``is_enabled`` so enabling a pack actually turns its modules on (and their
    proactive cues), on top of whatever ``PREFRONTAL_MODULES`` lists.
    """
    out: list[str] = []
    for pack in enabled_packs(settings):
        for key in pack.modules:
            if key not in out:
                out.append(key)
    return out


#: Coaching-state keys that arm the focus-balance guardrail — the weekly nudge
#: flag and any per-domain target. The guardrail itself (passive trip detection +
#: the "light on <sphere>" nudge) lives entirely in the ``trip_tracking`` module,
#: so a pack seeding these without that module enabled leaves them inert. Kept as
#: literals here (not imported from :mod:`prefrontal.focus_balance`) to keep the
#: packs layer dependency-free.
_FOCUS_BALANCE_SEED_KEYS = ("focus_balance_nudge",)
_FOCUS_BALANCE_SEED_PREFIXES = ("focus_target:",)


def focus_balance_seeding_gap(settings: Settings | None = None) -> list[str]:
    """Enabled packs that seed the focus-balance guardrail while its module is off.

    The focus-balance feature — the weekly ``focus_target:<domain>`` aims, the
    ``focus_balance_nudge`` heads-up, and the passive closed-loop trip detection it
    measures — lives entirely in the ``trip_tracking`` module. A pack that seeds
    those coaching defaults *without* ``trip_tracking`` enabled leaves the config
    inert: no trips are ever detected and the weekly nudge never fires. Returns the
    keys of the offending enabled packs (empty when there's no gap), so the app can
    warn once at startup — the shape of the bug the built-in packs used to ship
    with (targets seeded, module off).

    Args:
        settings: Settings to read the pack/module lists from. Defaults to
            :func:`prefrontal.config.get_settings`.
    """
    # Lazy import: the modules layer already cross-imports the packs registry, so
    # a top-level import here would risk a cycle at package-init time.
    from prefrontal.modules.registry import is_enabled as module_is_enabled

    if module_is_enabled("trip_tracking", settings):
        return []
    offenders: list[str] = []
    for pack in enabled_packs(settings):
        if any(
            key in _FOCUS_BALANCE_SEED_KEYS or key.startswith(_FOCUS_BALANCE_SEED_PREFIXES)
            for key in pack.coaching_defaults
        ):
            offenders.append(pack.key)
    return offenders


def enabled_situations(settings: Settings | None = None) -> list[SituationTool]:
    """Situation tools contributed by the enabled packs, in configured order.

    The registry side of the ``/packs/situations`` surface: only tools whose
    owning pack is enabled are returned, so a disabled pack's tools are invisible.
    First-seen wins if two enabled packs somehow declare the same tool ``key``.

    Args:
        settings: Settings to read the pack list from. Defaults to
            :func:`prefrontal.config.get_settings`.
    """
    out: list[SituationTool] = []
    seen: set[str] = set()
    for pack in enabled_packs(settings):
        for tool in pack.situations:
            if tool.key not in seen:
                seen.add(tool.key)
                out.append(tool)
    return out


def get_situation(key: str, settings: Settings | None = None) -> SituationTool | None:
    """Return an enabled situation tool by key, or ``None`` if unknown/disabled.

    ``None`` covers both "no such tool" and "its pack is off" — the caller (the
    router) turns either into a 404, since a tool behind a disabled pack should
    look the same as one that doesn't exist.
    """
    for tool in enabled_situations(settings):
        if tool.key == key:
            return tool
    return None


def resolve_pack_vocabulary(settings: Settings | None = None) -> PackVocabulary:
    """Merge the vocabulary of all enabled packs.

    Precedence follows ``PREFRONTAL_PACKS`` order: categories and commitment kinds
    are unioned (first-seen order preserved), and coaching defaults merge
    **earlier-pack-wins** on a key conflict — so if two packs default the same
    ``todo_window:…`` the earlier-listed pack's value stands (and seeding, being
    absent-only, preserves that same winner).

    Args:
        settings: Settings to read the pack list from. Defaults to
            :func:`prefrontal.config.get_settings`.
    """
    categories: list[str] = []
    kinds: list[str] = []
    coaching: dict[str, str] = {}
    for pack in enabled_packs(settings):
        for c in pack.categories:
            if c not in categories:
                categories.append(c)
        for k in pack.commitment_kinds:
            if k not in kinds:
                kinds.append(k)
        for key, value in pack.coaching_defaults.items():
            coaching.setdefault(key, value)  # earlier pack wins
    return PackVocabulary(
        categories=tuple(categories),
        commitment_kinds=tuple(kinds),
        coaching_defaults=coaching,
    )
