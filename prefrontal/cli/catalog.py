"""``prefrontal packs`` / ``prefrontal modules`` — list registered packs & modules.

Read-only catalog commands: what Context Packs and challenge-area modules exist
and which are enabled for the current configuration.
"""

from __future__ import annotations

import argparse

from prefrontal.config import get_settings
from prefrontal.modules import available, enabled_modules


def register(sub) -> None:
    """Attach the ``packs`` and ``modules`` subcommands to the top-level parser."""
    p_packs = sub.add_parser("packs", help="List Context Packs and their status.")
    p_packs.add_argument(
        "-v", "--verbose", action="store_true",
        help="Also list the modules, vocabulary, and defaults each pack contributes.",
    )
    p_packs.set_defaults(func=_cmd_packs)

    p_modules = sub.add_parser("modules", help="List challenge-area modules and their status.")
    p_modules.add_argument(
        "-v", "--verbose", action="store_true", help="Also list each module's interventions."
    )
    p_modules.add_argument(
        "--tutorial", nargs="?", const="", metavar="KEY", default=None,
        help="Print the new-user walkthrough (the /guide content) for all enabled "
        "modules, or just one when given a module KEY.",
    )
    p_modules.set_defaults(func=_cmd_modules)


def _cmd_modules(args: argparse.Namespace) -> int:
    """List available modules and whether each is enabled.

    Args:
        args: Parsed arguments; uses ``args.verbose`` to also list interventions,
            and ``args.tutorial`` to instead print the new-user walkthrough (the
            same content the in-app ``/guide`` page shows), optionally for a single
            module key.

    Returns:
        Process exit code (0 on success).
    """
    settings = get_settings()
    enabled = {m.key for m in enabled_modules(settings)}
    if getattr(args, "tutorial", None) is not None:
        wanted = args.tutorial or None  # "" (bare flag) → all enabled modules
        shown = 0
        for module in enabled_modules(settings):
            if wanted and module.key != wanted:
                continue
            shown += 1
            print(f"\n{module.title}")
            print("=" * len(module.title))
            for i, step in enumerate(module.tutorial(), start=1):
                print(f"\n{i}. {step.title}")
                for line in step.body.splitlines():
                    print(f"   {line}")
        if wanted and shown == 0:
            print(f"No enabled module '{wanted}'. See `prefrontal modules`.")
            return 1
        return 0
    for module in available():
        mark = "on " if module.key in enabled else "off"
        print(f"[{mark}] {module.key} — {module.title}")
        print(f"        {module.challenge}")
        if args.verbose:
            for iv in module.interventions():
                print(f"          - {iv.name} ({iv.status}): {iv.description}")
    return 0


def _cmd_packs(args: argparse.Namespace) -> int:
    """List available Context Packs and whether each is enabled.

    Args:
        args: Parsed arguments; uses ``args.verbose`` to also list the modules a
            pack switches on and the vocabulary/defaults it seeds.

    Returns:
        Process exit code (0 on success).
    """
    from prefrontal.packs import available as available_packs
    from prefrontal.packs import is_enabled as pack_is_enabled

    settings = get_settings()
    packs = available_packs()
    if not packs:
        print("No Context Packs registered.")
        return 0
    for pack in packs:
        mark = "on " if pack_is_enabled(pack.key, settings) else "off"
        print(f"[{mark}] {pack.key} — {pack.title}")
        if pack.description:
            print(f"        {pack.description}")
        if args.verbose:
            if pack.modules:
                print(f"          modules: {', '.join(pack.modules)}")
            if pack.categories:
                print(f"          categories: {', '.join(pack.categories)}")
            if pack.commitment_kinds:
                print(f"          commitment kinds: {', '.join(pack.commitment_kinds)}")
            for key, value in pack.coaching_defaults.items():
                print(f"          seeds {key} = {value}")
            for tool in pack.situations:
                print(f"          situation {tool.key} — {tool.description}")
    return 0


