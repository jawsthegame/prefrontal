"""Tests for Context Packs — the life-context composition layer.

Covers the registry (enabled by explicit opt-in, none by default), the Parent
pack's declared composition, the vocabulary merge + precedence rules, that an
enabled pack switches its modules on, and that its coaching defaults are seeded
(absent-only) for a fresh user.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from prefrontal.config import Settings
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.modules import enabled_modules
from prefrontal.modules.registry import is_enabled as module_is_enabled
from prefrontal.packs import (
    Pack,
    available,
    enabled_packs,
    is_enabled,
    pack_module_keys,
    register,
    resolve_pack_vocabulary,
)
from prefrontal.packs import get as get_pack

# -- registry ----------------------------------------------------------------


def test_parent_pack_is_registered_with_expected_composition():
    parent = get_pack("parent")
    assert parent.title == "Parent"
    assert set(parent.modules) == {"time_blindness", "task_paralysis"}
    assert "child" in parent.commitment_kinds
    assert "school" in parent.categories
    assert parent in available()


def test_caregiver_pack_is_registered_with_expected_composition():
    care = get_pack("caregiver")
    assert care.title == "Caregiver"
    # Leans on appointment timing, dreaded admin, and — distinctively — self-care.
    assert set(care.modules) == {"time_blindness", "task_paralysis", "self_care"}
    assert set(care.categories) == {"medical", "admin", "caregiving"}
    assert care.commitment_kinds == ("care",)  # the dedicated care-recipient kind
    # Arms the self-care checks and protects personal time; keeps admin in hours.
    assert care.coaching_defaults["self_care"] == "on"
    assert care.coaching_defaults["todo_window:admin"] == "09:00-17:00"
    assert care.coaching_defaults["focus_target:personal"] == "180"
    assert care in available()


def test_caregiver_pack_switches_on_self_care_and_arms_it(monkeypatch):
    """Enabling the pack turns the self_care module on *and* seeds self_care=on,
    both of which the basic-needs checks need to fire."""
    from prefrontal.modules.registry import is_enabled as module_is_enabled

    s = Settings(modules=("impulsivity",), packs=("caregiver",))
    keys = {m.key for m in enabled_modules(s)}
    assert {"time_blindness", "task_paralysis", "self_care"} <= keys
    assert module_is_enabled("self_care", s)


def test_packs_default_to_none_and_enable_by_opt_in():
    # Unlike modules (empty = all), no pack is enabled unless listed.
    assert enabled_packs(Settings(packs=())) == []
    assert [p.key for p in enabled_packs(Settings(packs=("parent",)))] == ["parent"]
    assert is_enabled("parent", Settings(packs=("parent",)))
    assert not is_enabled("parent", Settings(packs=()))
    # An unknown key is ignored, not an error.
    assert enabled_packs(Settings(packs=("nope",))) == []


def test_register_rejects_duplicate_key():
    with pytest.raises(ValueError, match="already registered"):
        register(get_pack("parent"))


# -- module composition ------------------------------------------------------


def test_enabled_pack_switches_its_modules_on():
    # A specific module list is extended by the pack's modules.
    s = Settings(modules=("impulsivity",), packs=("parent",))
    keys = {m.key for m in enabled_modules(s)}
    assert keys == {"impulsivity", "time_blindness", "task_paralysis"}
    assert pack_module_keys(s) == ["time_blindness", "task_paralysis"]
    # is_enabled honors the pack too, so the module's cues actually fire.
    assert module_is_enabled("time_blindness", s)
    assert not module_is_enabled("time_blindness", Settings(modules=("impulsivity",)))


def test_all_modules_enabled_ignores_packs():
    # Empty module list already means "all"; packs add nothing (and don't error).
    s = Settings(modules=(), packs=("parent",))
    assert {m.key for m in enabled_modules(s)} == {m.key for m in enabled_modules(Settings())}


# -- vocabulary merge + precedence -------------------------------------------


def test_resolve_vocabulary_unions_and_earlier_pack_wins():
    a = register(
        Pack(
            key="_test_a",
            title="A",
            categories=("shared", "a_only"),
            commitment_kinds=("child",),
            coaching_defaults=MappingProxyType({"todo_window:shared": "09:00-10:00"}),
        )
    )
    b = register(
        Pack(
            key="_test_b",
            title="B",
            categories=("shared", "b_only"),
            coaching_defaults=MappingProxyType({"todo_window:shared": "11:00-12:00"}),
        )
    )
    try:
        # Order in PREFRONTAL_PACKS decides precedence: a listed first wins.
        vocab = resolve_pack_vocabulary(Settings(packs=("_test_a", "_test_b")))
        assert vocab.categories == ("shared", "a_only", "b_only")  # union, first-seen order
        assert vocab.commitment_kinds == ("child",)
        assert vocab.coaching_defaults["todo_window:shared"] == "09:00-10:00"  # a wins
        # Reversing the order flips the winner.
        rev = resolve_pack_vocabulary(Settings(packs=("_test_b", "_test_a")))
        assert rev.coaching_defaults["todo_window:shared"] == "11:00-12:00"
    finally:
        from prefrontal.packs.registry import _REGISTRY

        _REGISTRY.pop(a.key, None)
        _REGISTRY.pop(b.key, None)


# -- seeding -----------------------------------------------------------------


def test_enabled_pack_seeds_coaching_defaults_absent_only(monkeypatch):
    monkeypatch.setenv("PREFRONTAL_PACKS", "parent")
    from prefrontal.config import get_settings
    from prefrontal.memory.store import seed_user_state

    get_settings.cache_clear()
    try:
        with MemoryStore.open(":memory:") as store:
            user, _ = provision_user(store, "p", display_name="P", is_operator=True)
            scoped = store.scoped(user["id"])
            # All six of the parent pack's coaching defaults are seeded on provision.
            assert scoped.get_state("todo_window:school") == "08:00-15:00"
            assert scoped.get_state("todo_window:childcare") == "06:00-20:00"
            assert scoped.get_state("focus_balance_nudge") == "1"
            assert scoped.get_state("focus_target:kids") == "300"
            assert scoped.get_state("focus_target:home") == "120"
            assert scoped.get_state("focus_target:personal") == "120"
            # Absent-only: a value the user has since changed survives a re-seed
            # (the actual guarantee the test name claims — previously unexercised).
            scoped.set_state("focus_target:kids", "60", source="explicit")
            seed_user_state(scoped)
            assert scoped.get_state("focus_target:kids") == "60"
    finally:
        get_settings.cache_clear()


def test_caregiver_pack_arms_self_care_over_the_module_default(monkeypatch):
    """A pack default beats a module default (pack > module): the Caregiver pack
    seeds self_care=on even though the self_care module's own default is off."""
    monkeypatch.setenv("PREFRONTAL_PACKS", "caregiver")
    from prefrontal.config import get_settings

    get_settings.cache_clear()
    try:
        with MemoryStore.open(":memory:") as store:
            user, _ = provision_user(store, "c", display_name="C", is_operator=True)
            scoped = store.scoped(user["id"])
            assert scoped.get_state("self_care") == "on"  # pack wins over module 'off'
            assert scoped.get_state("todo_window:medical") == "08:00-17:00"
            assert scoped.get_state("focus_target:personal") == "180"
    finally:
        get_settings.cache_clear()


def test_no_pack_leaves_self_care_at_the_module_default(monkeypatch):
    """Without the pack, the self_care module's own default (off) stands."""
    monkeypatch.delenv("PREFRONTAL_PACKS", raising=False)
    from prefrontal.config import get_settings

    get_settings.cache_clear()
    try:
        with MemoryStore.open(":memory:") as store:
            user, _ = provision_user(store, "n", display_name="N", is_operator=True)
            assert store.scoped(user["id"]).get_state("self_care") == "off"
    finally:
        get_settings.cache_clear()


def test_no_pack_seeds_nothing(monkeypatch):
    monkeypatch.delenv("PREFRONTAL_PACKS", raising=False)
    from prefrontal.config import get_settings

    get_settings.cache_clear()
    try:
        with MemoryStore.open(":memory:") as store:
            user, _ = provision_user(store, "p", display_name="P", is_operator=True)
            scoped = store.scoped(user["id"])
            assert scoped.get_state("todo_window:school") is None
    finally:
        get_settings.cache_clear()
