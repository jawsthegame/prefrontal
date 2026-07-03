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
from prefrontal.modules.task_paralysis import repeat_stalled_tasks
from tests.conftest import scoped_default

BUILTIN_KEYS = {"time_blindness", "task_paralysis", "hyperfocus", "impulsivity"}


@pytest.fixture()
def store():
    """Yield a MemoryStore backed by a fresh in-memory, schema-initialized DB."""
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


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


def test_task_paralysis_interventions_all_active():
    """All three initiation interventions are wired (status active)."""
    ivs = {i.name: i.status for i in get("task_paralysis").interventions()}
    assert ivs == {
        "tiny_first_step": "active",
        "auto_decompose": "active",
        "body_double_nudge": "active",
    }


def test_time_blindness_intervention_statuses_are_honest():
    """Estimate correction + departure timing are wired; elapsed-time callouts aren't.

    The status flags must reflect reality (they drive ``prefrontal modules -v``):
    ``departure_buffer`` is delivered by the live departure planner
    (bias-adjusted leave-by + heads_up/soon/go escalation, ``/webhooks/departure``),
    while ``elapsed_time_callouts`` has no wiring yet, so it stays ``planned``.
    """
    ivs = {i.name: i.status for i in get("time_blindness").interventions()}
    assert ivs == {
        "estimate_correction": "active",
        "departure_buffer": "active",
        "elapsed_time_callouts": "planned",
    }


def test_repeat_stalled_tasks_flags_repeat_misses_but_not_resolved():
    """Two+ misses on a task flag it; a task later completed drops off."""
    episodes = [
        {"id": 1, "context": "todo dropped: Call the dentist", "outcome": "miss"},
        {"id": 2, "context": "todo dropped: Call the dentist", "outcome": "miss"},
        {"id": 3, "context": "todo dropped: File taxes", "outcome": "miss"},
        {"id": 4, "context": "todo dropped: File taxes", "outcome": "miss"},
        {"id": 5, "context": "todo done: File taxes", "outcome": "success"},
        {"id": 6, "context": "outing: coffee", "outcome": "miss"},  # not a todo
    ]
    stuck = repeat_stalled_tasks(episodes)
    titles = [s["title"] for s in stuck]
    assert titles == ["Call the dentist"]  # File taxes resolved; outing ignored
    assert stuck[0]["misses"] == 2 and stuck[0]["attempts"] == 2


def test_repeat_stalled_tasks_respects_min_misses():
    """A single miss isn't yet a body-double signal."""
    episodes = [{"id": 1, "context": "todo dropped: Email Sam", "outcome": "miss"}]
    assert repeat_stalled_tasks(episodes) == []
    assert repeat_stalled_tasks(episodes, min_misses=1)[0]["title"] == "Email Sam"


def test_task_paralysis_profile_names_stuck_tasks(store):
    """The profile section names a task the user keeps bailing on."""
    for _ in range(2):
        store.log_episode(
            "task", outcome="miss", context="todo dropped: Renew passport"
        )
    get("task_paralysis").seed(store)
    section = get("task_paralysis").profile_section(store)
    assert section is not None
    assert "Keeps bailing on" in section
    assert "Renew passport" in section
