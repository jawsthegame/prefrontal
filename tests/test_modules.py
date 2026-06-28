"""Tests for the challenge-area module system.

Covers the registry (availability, enable/disable via settings), per-module
seeding and profile contributions, and the summarizer's integration of module
sections. Built-in modules are imported for their registration side effects.
"""

from __future__ import annotations

import pytest

import prefrontal.modules  # noqa: F401  (registers built-in modules on import)
from prefrontal.config import Settings
from prefrontal.memory.store import MemoryStore
from prefrontal.memory.summarizer import build_profile
from prefrontal.modules import available, enabled_modules, get

BUILTIN_KEYS = {"time_blindness", "task_paralysis", "hyperfocus", "impulsivity"}


@pytest.fixture()
def store():
    """Yield a MemoryStore backed by a fresh in-memory, schema-initialized DB."""
    with MemoryStore.open(":memory:") as s:
        yield s


def test_all_builtins_registered():
    """The four built-in modules are all registered."""
    keys = {m.key for m in available()}
    assert BUILTIN_KEYS <= keys


def test_empty_settings_enables_everything():
    """No explicit module list means every module is enabled."""
    enabled = {m.key for m in enabled_modules(Settings(modules=()))}
    assert BUILTIN_KEYS <= enabled


def test_explicit_list_filters_and_orders():
    """An explicit list enables only those modules, in order, ignoring unknowns."""
    settings = Settings(modules=("hyperfocus", "bogus", "time_blindness"))
    enabled = [m.key for m in enabled_modules(settings)]
    assert enabled == ["hyperfocus", "time_blindness"]


def test_module_seed_is_non_clobbering(store):
    """seed() writes its defaults but never overwrites an existing value."""
    store.set_state("departure_buffer_minutes", "25", source="explicit")
    get("time_blindness").seed(store)
    # Existing explicit value preserved...
    assert store.get_state("departure_buffer_minutes") == "25"
    # ...but a missing default from the same module is now present.
    assert store.get_state("time_estimation_bias") is not None


def test_each_module_declares_interventions():
    """Every built-in module declares at least one intervention."""
    for module in available():
        assert module.interventions(), f"{module.key} has no interventions"


def test_hyperfocus_profile_mentions_good_vs_bad(store):
    """Hyperfocus is the asymmetric case — its profile must say so."""
    get("hyperfocus").seed(store)
    section = get("hyperfocus").profile_section(store)
    assert section is not None
    assert "good vs bad" in section.lower()


def test_profile_includes_module_sections(store):
    """build_profile renders an Active modules section with module titles."""
    for module in available():
        module.seed(store)
    profile = build_profile(store, modules=available())
    assert "## Active modules" in profile
    assert "### Time Blindness" in profile
    assert "### Hyperfocus" in profile


def test_profile_can_omit_modules(store):
    """Passing an empty module list omits the module sections."""
    profile = build_profile(store, modules=[])
    assert "## Active modules" not in profile


def test_task_paralysis_reports_stall_rate(store):
    """With mostly-missed tasks, the module reports a high stall rate."""
    for _ in range(3):
        store.log_episode("task", acknowledged=False, outcome="miss")
    store.log_episode("task", acknowledged=True, outcome="success")
    get("task_paralysis").seed(store)
    section = get("task_paralysis").profile_section(store)
    assert section is not None
    assert "stall" in section.lower()
