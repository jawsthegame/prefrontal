#!/usr/bin/env python3
"""Fold ``changelog.d/`` fragments into ``CHANGELOG.md`` — conflict-free changelogs.

Every change that used to prepend a bullet to ``CHANGELOG.md``'s
``## Recently shipped`` section now drops a standalone fragment file under
``changelog.d/`` instead (see ``changelog.d/README.md``). Two branches adding two
*different* files never conflict, and a branch only ever *adds* a file — so
rebases stop conflicting on the changelog. This script is the one place the
shared ``CHANGELOG.md`` is touched: run it periodically (not per PR) to move the
accumulated fragments into the changelog, newest first, and delete them.

Usage::

    python scripts/collate_changelog.py            # fold fragments in, delete them
    python scripts/collate_changelog.py --check     # list pending; non-zero if any
    python scripts/collate_changelog.py --keep      # fold in but keep the fragments

A fragment is any ``*.md`` in ``changelog.d/`` except ``README.md``. Ordering is
by filename descending, so a ``YYYY-MM-DD-slug.md`` prefix sorts newest-first.
The fragment's whole body is inserted verbatim, so it can be one bullet or a few.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
FRAGMENT_DIR = REPO_ROOT / "changelog.d"
SECTION_HEADING = "## Recently shipped"
#: Fragment filenames that are documentation, not changelog entries.
RESERVED = {"README.md"}


def fragment_paths(fragment_dir: Path = FRAGMENT_DIR) -> list[Path]:
    """Return the pending fragment files, newest-first by filename.

    Args:
        fragment_dir: The ``changelog.d`` directory to scan.

    Returns:
        ``*.md`` files (excluding :data:`RESERVED`), sorted filename-descending so
        a ``YYYY-MM-DD-`` prefix orders newest-first.
    """
    if not fragment_dir.is_dir():
        return []
    frags = [
        p
        for p in fragment_dir.glob("*.md")
        if p.name not in RESERVED
    ]
    return sorted(frags, key=lambda p: p.name, reverse=True)


def collate_text(changelog: str, fragments: list[str]) -> str:
    """Insert ``fragments`` at the top of the changelog's Recently-shipped list.

    Pure text transform (no filesystem), so it's unit-testable. Each fragment is
    inserted verbatim, in the given order, above the section's existing entries,
    separated by a blank line.

    Args:
        changelog: The full ``CHANGELOG.md`` text.
        fragments: Fragment bodies, already ordered newest-first.

    Returns:
        The new changelog text. Unchanged when ``fragments`` is empty.

    Raises:
        ValueError: If the ``## Recently shipped`` heading isn't found.
    """
    if not fragments:
        return changelog
    lines = changelog.splitlines()
    try:
        heading_idx = next(
            i for i, line in enumerate(lines) if line.strip() == SECTION_HEADING
        )
    except StopIteration as exc:
        raise ValueError(f"{SECTION_HEADING!r} heading not found in changelog") from exc
    # Insert right after the heading and the single blank line that follows it, so
    # the new entries sit at the very top of the list (above the current newest).
    insert_at = heading_idx + 1
    if insert_at < len(lines) and lines[insert_at].strip() == "":
        insert_at += 1

    block: list[str] = []
    for fragment in fragments:
        block.extend(fragment.strip("\n").splitlines())
        block.append("")  # blank line between entries
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    return "\n".join(new_lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. See the module docstring for usage."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="List pending fragments and exit non-zero if any exist (for CI).",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Fold fragments into CHANGELOG.md but leave the fragment files.",
    )
    args = parser.parse_args(argv)

    paths = fragment_paths(FRAGMENT_DIR)
    if not paths:
        print("No changelog fragments pending.")
        return 0

    if args.check:
        print(f"{len(paths)} changelog fragment(s) pending:")
        for path in paths:
            print(f"  - {path.relative_to(REPO_ROOT)}")
        return 1

    fragments = [path.read_text(encoding="utf-8") for path in paths]
    new_text = collate_text(CHANGELOG.read_text(encoding="utf-8"), fragments)
    CHANGELOG.write_text(new_text, encoding="utf-8")
    print(f"Folded {len(paths)} fragment(s) into {CHANGELOG.relative_to(REPO_ROOT)}:")
    for path in paths:
        print(f"  - {path.relative_to(REPO_ROOT)}")
        if not args.keep:
            path.unlink()
    if args.keep:
        print("(--keep: fragment files left in place)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
