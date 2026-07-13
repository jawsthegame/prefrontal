"""The ``changelog.d`` fragment collator (``scripts/collate_changelog.py``).

Fragments keep the changelog out of every rebase: each change adds its own file
under ``changelog.d/`` and ``collate_changelog.py`` folds them into
``CHANGELOG.md`` newest-first. These tests pin the ordering, the insertion point,
the ``README.md`` exclusion, and the ``--check`` / delete behavior.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "collate_changelog.py"
_spec = importlib.util.spec_from_file_location("collate_changelog", _SCRIPT)
collate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collate)


CHANGELOG = """\
# Changelog

Intro paragraph.

## Recently shipped

- **Older thing** ✅ — shipped a while ago.
"""


# -- collate_text (pure) -----------------------------------------------------


def test_collate_inserts_at_top_of_section_in_order():
    out = collate.collate_text(
        CHANGELOG,
        ["- **Newest** ✅ — the latest.", "- **Middle** ✅ — before that."],
    )
    body = out.split("## Recently shipped", 1)[1]
    # Fragments land above the existing entry, in the order given.
    assert body.index("Newest") < body.index("Middle") < body.index("Older thing")
    # The intro above the section is untouched.
    assert out.startswith("# Changelog\n\nIntro paragraph.")


def test_collate_preserves_multiline_fragment_verbatim():
    frag = "- **Multi** ✅ — line one\n  continued on line two."
    out = collate.collate_text(CHANGELOG, [frag])
    assert "line one\n  continued on line two." in out


def test_collate_no_fragments_is_a_noop():
    assert collate.collate_text(CHANGELOG, []) == CHANGELOG


def test_collate_without_section_heading_raises():
    with pytest.raises(ValueError, match="Recently shipped"):
        collate.collate_text("# Changelog\n\nNo section here.\n", ["- x"])


# -- fragment_paths + CLI (filesystem) ---------------------------------------


def _write(dir_: Path, name: str, body: str) -> None:
    (dir_ / name).write_text(body, encoding="utf-8")


def test_fragment_paths_sorts_newest_first_and_skips_readme(tmp_path):
    _write(tmp_path, "2026-01-01-old.md", "- old")
    _write(tmp_path, "2026-07-13-new.md", "- new")
    _write(tmp_path, "README.md", "# docs")  # excluded
    names = [p.name for p in collate.fragment_paths(tmp_path)]
    assert names == ["2026-07-13-new.md", "2026-01-01-old.md"]


def test_cli_folds_in_and_deletes_fragments(tmp_path, monkeypatch, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    fragdir = tmp_path / "changelog.d"
    fragdir.mkdir()
    _write(fragdir, "2026-07-13-feature.md", "- **Feature** ✅ — new.")
    _write(fragdir, "README.md", "# docs")
    monkeypatch.setattr(collate, "CHANGELOG", changelog)
    monkeypatch.setattr(collate, "FRAGMENT_DIR", fragdir)
    monkeypatch.setattr(collate, "REPO_ROOT", tmp_path)

    assert collate.main([]) == 0
    text = changelog.read_text(encoding="utf-8")
    assert "- **Feature** ✅ — new." in text
    assert text.index("Feature") < text.index("Older thing")
    # The fragment is consumed; the README survives.
    assert not (fragdir / "2026-07-13-feature.md").exists()
    assert (fragdir / "README.md").exists()


def test_cli_check_lists_pending_and_returns_nonzero(tmp_path, monkeypatch):
    fragdir = tmp_path / "changelog.d"
    fragdir.mkdir()
    _write(fragdir, "2026-07-13-feature.md", "- **Feature** ✅ — new.")
    monkeypatch.setattr(collate, "FRAGMENT_DIR", fragdir)
    monkeypatch.setattr(collate, "REPO_ROOT", tmp_path)
    # --check reports without mutating and signals "pending" via a non-zero exit.
    assert collate.main(["--check"]) == 1
    assert (fragdir / "2026-07-13-feature.md").exists()


def test_cli_keep_folds_in_but_retains_fragments(tmp_path, monkeypatch):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    fragdir = tmp_path / "changelog.d"
    fragdir.mkdir()
    _write(fragdir, "2026-07-13-feature.md", "- **Feature** ✅ — new.")
    monkeypatch.setattr(collate, "CHANGELOG", changelog)
    monkeypatch.setattr(collate, "FRAGMENT_DIR", fragdir)
    monkeypatch.setattr(collate, "REPO_ROOT", tmp_path)

    assert collate.main(["--keep"]) == 0
    assert "Feature" in changelog.read_text(encoding="utf-8")
    assert (fragdir / "2026-07-13-feature.md").exists()  # kept


def test_cli_no_fragments_is_a_noop(tmp_path, monkeypatch, capsys):
    fragdir = tmp_path / "changelog.d"
    fragdir.mkdir()
    monkeypatch.setattr(collate, "FRAGMENT_DIR", fragdir)
    monkeypatch.setattr(collate, "REPO_ROOT", tmp_path)
    assert collate.main([]) == 0
    assert "No changelog fragments pending." in capsys.readouterr().out
