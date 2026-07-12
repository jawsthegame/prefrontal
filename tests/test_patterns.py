"""Tests for the pattern-computation pass (the learning engine).

Covers the pure compute_patterns / compute_bias functions with synthetic
episodes, the confidence estimator, and the end-to-end recompute_patterns against
an in-memory store (including idempotency and bias write-back).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from prefrontal.memory.patterns import (
    ACTIVITY_BIAS_PREFIX,
    CATEGORY_BIAS_PREFIX,
    ENERGY_BIAS_PREFIX,
    TYPE_BIAS_PREFIX,
    _make_half_life_resolver,
    channel_calibration,
    compute_bias,
    compute_bias_by_activity,
    compute_bias_by_band,
    compute_bias_by_category,
    compute_bias_by_energy,
    compute_bias_by_type,
    compute_confidence,
    compute_early_start_threshold,
    compute_patterns,
    decay_bias_toward_neutral,
    decay_channel_rate_toward_pooled,
    decay_weight,
    derive_band_half_lives,
    derive_context_half_lives,
    derive_half_life,
    episode_activity,
    filter_to_window,
    pooled_channel_rate,
    recompute_patterns,
    resolve_bias,
    task_bias_resolver,
    time_of_day_band,
    within_window,
)
from prefrontal.memory.store import MemoryStore
from tests.conftest import scoped_default


def _ep(**kw):
    """Build an episode dict with sensible defaults for the fields we ignore."""
    base = {
        "episode_type": "task",
        "predicted_value": None,
        "actual_value": None,
        "acknowledged": None,
        "channel": None,
        "outcome": None,
    }
    base.update(kw)
    return base


def _by_key(results, pattern_type):
    """Index results of a given type by context_key."""
    return {r.context_key: r for r in results if r.pattern_type == pattern_type}


# -- confidence --------------------------------------------------------------


def test_confidence_grows_with_samples():
    """Confidence is 0 at no data and approaches 1 as n grows."""
    assert compute_confidence(0) == 0.0
    assert compute_confidence(5, k=5) == pytest.approx(0.5)
    assert compute_confidence(45, k=5) == pytest.approx(0.9)
    assert compute_confidence(10) < compute_confidence(100)


# -- time_estimation ---------------------------------------------------------


def test_time_estimation_variance_and_means():
    """Mean predicted/observed and a positive variance for underestimates."""
    episodes = [
        _ep(episode_type="departure", predicted_value=15, actual_value=21),
        _ep(episode_type="departure", predicted_value=15, actual_value=23),
        _ep(episode_type="task", predicted_value=30, actual_value=30),
    ]
    te = _by_key(compute_patterns(episodes), "time_estimation")
    assert te["departure"].observed_value == pytest.approx(22.0)
    assert te["departure"].predicted_value == pytest.approx(15.0)
    assert te["departure"].variance == pytest.approx(7.0)  # underestimate
    assert te["departure"].sample_size == 2
    assert te["task"].variance == pytest.approx(0.0)


def test_time_estimation_ignores_incomplete_pairs():
    """Episodes missing predicted or actual don't contribute."""
    episodes = [
        _ep(episode_type="departure", predicted_value=15, actual_value=None),
        _ep(episode_type="departure", predicted_value=None, actual_value=20),
    ]
    assert _by_key(compute_patterns(episodes), "time_estimation") == {}


# -- channel_response --------------------------------------------------------


def test_channel_response_ack_rate():
    """Acknowledgement rate is computed per channel."""
    episodes = [
        _ep(channel="notification", acknowledged=True),
        _ep(channel="notification", acknowledged=True),
        _ep(channel="notification", acknowledged=False),
        _ep(channel="tts", acknowledged=True),
    ]
    cr = _by_key(compute_patterns(episodes), "channel_response")
    assert cr["notification"].observed_value == pytest.approx(2 / 3, abs=1e-3)
    assert cr["tts"].observed_value == pytest.approx(1.0)


# -- drift -------------------------------------------------------------------


def test_drift_score_weights_outcomes():
    """Drift scores success=0, partial=0.5, miss=1.0."""
    episodes = [
        _ep(episode_type="task", outcome="success"),
        _ep(episode_type="task", outcome="miss"),
        _ep(episode_type="task", outcome="partial"),
    ]
    drift = _by_key(compute_patterns(episodes), "drift")
    assert drift["task"].observed_value == pytest.approx(0.5)  # (0 + 1 + 0.5)/3


# -- context_switch ----------------------------------------------------------


def test_context_switch_means_impulses_and_deferrals():
    """Per-session `switch` episodes → mean impulses, deferrals, and honored."""
    episodes = [
        _ep(episode_type="switch", context="focus", predicted_value=4, actual_value=3),
        _ep(episode_type="switch", context="focus", predicted_value=2, actual_value=1),
        # A clean 0-impulse block still counts toward the per-session mean.
        _ep(episode_type="switch", context="focus", predicted_value=0, actual_value=0),
    ]
    cs = _by_key(compute_patterns(episodes), "context_switch")
    assert cs["focus"].observed_value == pytest.approx(2.0)  # (4+2+0)/3 impulses
    assert cs["focus"].predicted_value == pytest.approx(4 / 3, abs=1e-3)  # deferrals
    assert cs["focus"].variance == pytest.approx(2.0 - 4 / 3, abs=1e-3)  # honored
    assert cs["focus"].sample_size == 3


def test_context_switch_ignores_non_switch_episodes():
    """Only `switch` episodes feed the pattern; a `task` close doesn't."""
    episodes = [_ep(episode_type="task", predicted_value=30, actual_value=30)]
    assert _by_key(compute_patterns(episodes), "context_switch") == {}


# -- bias --------------------------------------------------------------------


def test_compute_bias_ratio():
    """Bias is Σactual / Σpredicted, rounded to 2dp."""
    episodes = [
        _ep(predicted_value=10, actual_value=14),
        _ep(predicted_value=10, actual_value=14),
        _ep(predicted_value=10, actual_value=14),
    ]
    assert compute_bias(episodes) == pytest.approx(1.4)


def test_compute_bias_needs_minimum_samples():
    """Too few pairs -> None (don't trust a noisy multiplier)."""
    assert compute_bias([_ep(predicted_value=10, actual_value=20)]) is None


def test_compute_bias_ignores_switch_episodes():
    """`switch` episodes store impulse *counts*, not minutes, so they must not
    feed the duration bias — otherwise they'd drag the global multiplier."""
    clean = [_ep(predicted_value=60, actual_value=60) for _ in range(6)]  # ratio 1.0
    switch = [
        _ep(episode_type="switch", context="focus", predicted_value=5, actual_value=1)
        for _ in range(6)  # ratio 0.2 — would pull the bias below 1.0 if counted
    ]
    assert compute_bias(clean) == pytest.approx(1.0)
    assert compute_bias(clean + switch) == pytest.approx(1.0)  # switch ignored


def test_time_estimation_patterns_ignore_switch_episodes():
    """A `switch` block never produces a bogus `switch` time-estimation pattern."""
    episodes = [
        _ep(episode_type="switch", context="focus", predicted_value=5, actual_value=2)
        for _ in range(4)
    ]
    assert _by_key(compute_patterns(episodes), "time_estimation") == {}


# -- end to end --------------------------------------------------------------


def test_recompute_persists_patterns_and_bias():
    """recompute_patterns writes pattern rows and refreshes the bias value."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for _ in range(4):
            store.log_episode(
                "departure",
                predicted_value=10,
                actual_value=14,
                channel="notification",
                acknowledged=True,
                outcome="miss",
            )
        summary = recompute_patterns(store)

        assert summary.episodes == 4
        assert summary.bias == pytest.approx(1.4)
        assert summary.by_type["time_estimation"] == 1

        te = store.get_patterns("time_estimation")
        assert te[0]["context_key"] == "departure"
        assert te[0]["variance"] == pytest.approx(4.0)
        assert te[0]["sample_size"] == 4
        # Bias written back to coaching state.
        assert store.get_state("time_estimation_bias") == "1.4"


def test_recompute_is_idempotent():
    """Running twice on the same data converges (no duplicate rows)."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for _ in range(3):
            store.log_episode("task", predicted_value=20, actual_value=30)
        recompute_patterns(store)
        recompute_patterns(store)
        te = store.get_patterns("time_estimation")
        assert len(te) == 1
        assert te[0]["sample_size"] == 3


def test_recompute_with_no_episodes():
    """No episodes -> no patterns, bias left as the seeded default."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        summary = recompute_patterns(store)
        assert summary.episodes == 0
        assert summary.patterns == 0
        assert summary.bias is None
        assert store.get_state("time_estimation_bias") == "1.4"  # untouched seed


# -- recency weighting / decay -----------------------------------------------


def test_decay_weight_halves_each_half_life():
    now = datetime(2026, 1, 31, 0, 0, 0)
    assert decay_weight("2026-01-31 00:00:00", now, 30) == pytest.approx(1.0)  # today
    assert decay_weight("2026-01-01 00:00:00", now, 30) == pytest.approx(0.5, abs=1e-3)  # 30d
    assert decay_weight("2025-12-02 00:00:00", now, 30) == pytest.approx(0.25, abs=1e-2)  # 60d
    # Disabled (0/None) or a missing/unparseable timestamp → no decay.
    assert decay_weight("2020-01-01 00:00:00", now, 0) == 1.0
    assert decay_weight("2020-01-01 00:00:00", now, None) == 1.0
    assert decay_weight(None, now, 30) == 1.0


def test_compute_patterns_recency_weights_recent_drift():
    """A stale miss barely counts against a fresh success once decay is on."""
    now = datetime(2026, 2, 1, 0, 0, 0)
    episodes = [
        _ep(episode_type="task", outcome="miss", timestamp="2025-08-01 00:00:00"),  # ~6mo old
        _ep(episode_type="task", outcome="success", timestamp="2026-02-01 00:00:00"),  # today
    ]
    flat = _by_key(compute_patterns(episodes, now=now, half_life_days=0), "drift")["task"]
    assert flat.observed_value == pytest.approx(0.5)  # equal weight: mean(1.0, 0.0)
    decayed = _by_key(compute_patterns(episodes, now=now, half_life_days=30), "drift")["task"]
    assert decayed.observed_value < 0.1  # the old miss is heavily discounted
    # Effective sample size (summed weights) is well under the raw count of 2.
    assert decayed.sample_size == 2 and decayed.confidence < flat.confidence


def test_compute_bias_recency_weighted():
    """A recent run of on-time estimates pulls the multiplier back off an old overrun."""
    now = datetime(2026, 2, 1, 0, 0, 0)
    episodes = [
        _ep(predicted_value=10, actual_value=20, timestamp="2025-08-01 00:00:00"),  # old 2× overrun
        _ep(predicted_value=10, actual_value=10, timestamp="2026-02-01 00:00:00"),  # recent on time
        _ep(predicted_value=10, actual_value=10, timestamp="2026-02-01 00:00:00"),  # recent on time
    ]
    flat = compute_bias(episodes, now=now, half_life_days=0)
    assert flat == pytest.approx(1.33, abs=0.01)  # (20+10+10)/30
    decayed = compute_bias(episodes, now=now, half_life_days=30)
    assert decayed < flat and decayed == pytest.approx(1.0, abs=0.05)  # old overrun discounted


def test_recompute_half_life_key_controls_decay():
    """The learning_half_life_days coaching key switches decay on/off end to end."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.log_episode("task", outcome="miss", timestamp="2020-01-01 00:00:00")  # ancient
        store.log_episode("task", outcome="success")  # now
        store.log_episode("task", outcome="success")  # now

        store.set_state("learning_half_life_days", "0")  # equal weight
        recompute_patterns(store)
        flat = store.get_patterns("drift")[0]["observed_value"]
        assert flat == pytest.approx(1 / 3, abs=0.01)  # one miss of three

        store.set_state("learning_half_life_days", "30")  # decay on
        recompute_patterns(store)
        decayed = store.get_patterns("drift")[0]["observed_value"]
        assert decayed < flat  # the ancient miss is now nearly weightless


# -- bias calibration (does the adaptation help? §4) -------------------------


def _dated(pred, act, day):
    return _ep(predicted_value=pred, actual_value=act, timestamp=f"2026-01-{day:02d} 09:00:00")


def test_bias_calibration_insufficient_data():
    from prefrontal.memory.patterns import bias_calibration

    cal = bias_calibration([_dated(10, 15, d) for d in range(1, 5)])  # 4 < min 6
    assert cal.status == "insufficient" and cal.samples == 4 and not cal.helps


def test_bias_calibration_helps_when_bias_reduces_error():
    from prefrontal.memory.patterns import bias_calibration

    # A steady 1.5× underestimate throughout: the bias learned from the older
    # slice zeroes the error on the newer slice.
    cal = bias_calibration([_dated(10, 15, d) for d in range(1, 10)])  # 9 pairs
    assert cal.status == "ok"
    assert cal.train_bias == pytest.approx(1.5)
    assert cal.raw_error == pytest.approx(5.0)
    assert cal.adjusted_error == pytest.approx(0.0)
    assert cal.improvement == pytest.approx(5.0) and cal.helps is True


def test_bias_calibration_flags_a_stale_bias_that_hurts():
    from prefrontal.memory.patterns import bias_calibration

    # You *used* to run over (older pairs underestimate → bias > 1), but lately
    # you're on time. Applying the old bias now overshoots — it should NOT help.
    older = [_dated(10, 15, d) for d in range(1, 7)]      # train: 1.5× underestimate
    recent = [_dated(10, 10, d) for d in range(7, 10)]    # test: on time
    cal = bias_calibration(older + recent)
    assert cal.status == "ok"
    assert cal.raw_error == pytest.approx(0.0)      # raw predictions were already spot on
    assert cal.adjusted_error == pytest.approx(5.0)  # the 1.5× bias overshoots
    assert cal.improvement == pytest.approx(-5.0) and cal.helps is False


def test_recompute_persists_calibration_verdict():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for d in range(1, 10):
            store.log_episode(
                "task", predicted_value=10, actual_value=15,
                timestamp=f"2026-01-{d:02d} 09:00:00",
            )
        summary = recompute_patterns(store)
        assert summary.calibration is not None and summary.calibration.status == "ok"
        assert summary.calibration.helps is True
        assert store.get_state("bias_calibration_helps") == "true"
        assert store.get_state("bias_calibration_samples") == "3"


# -- bias auto-act on a "not helping" verdict (§4) ---------------------------


def test_decay_bias_toward_neutral_shrinks_deviation():
    assert decay_bias_toward_neutral(1.4, 0.5) == 1.2   # half the deviation removed
    assert decay_bias_toward_neutral(0.6, 0.5) == 0.8   # works below 1.0 too
    assert decay_bias_toward_neutral(1.4, 1.0) == 1.0   # full reset to neutral
    assert decay_bias_toward_neutral(1.4, 0.0) == 1.4   # disabled ⇒ unchanged
    # factor is clamped, so it can only ever move *toward* 1.0, never past/away.
    assert decay_bias_toward_neutral(1.4, 5.0) == 1.0
    assert decay_bias_toward_neutral(1.4, -3.0) == 1.4


def _stale_hurting_store(store):
    """Seed the 'used to run over, now on time' history from the calibration test.

    Global bias lands at 1.33 (>1) but the walk-forward check says it no longer
    helps recent estimates. Decay is disabled so ``compute_bias`` is exact.
    """
    store.set_state("learning_half_life_days", "0")
    for d in range(1, 7):  # older: 1.5× underestimate
        store.log_episode(
            "task", predicted_value=10, actual_value=15, timestamp=f"2026-01-{d:02d} 09:00:00"
        )
    for d in range(7, 10):  # recent: on time
        store.log_episode(
            "task", predicted_value=10, actual_value=10, timestamp=f"2026-01-{d:02d} 09:00:00"
        )


def test_recompute_auto_decays_a_bias_that_is_not_helping():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        _stale_hurting_store(store)
        summary = recompute_patterns(store)

        assert summary.calibration is not None and summary.calibration.helps is False
        # The freshly-computed 1.33 was pulled toward 1.0 before it was persisted.
        assert summary.bias_pre_decay == 1.33
        assert summary.bias == decay_bias_toward_neutral(1.33)
        assert store.get_state("time_estimation_bias") == str(summary.bias)
        assert store.get_state("bias_calibration_decayed") == "true"
        assert summary.bias < summary.bias_pre_decay  # strictly toward neutral


def test_recompute_can_disable_auto_act():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        _stale_hurting_store(store)
        store.set_state("bias_decay_on_miss", "0")  # opt back into report-only
        summary = recompute_patterns(store)

        assert summary.calibration.helps is False
        assert summary.bias_pre_decay is None
        assert summary.bias == 1.33  # left exactly as computed
        assert store.get_state("time_estimation_bias") == "1.33"
        assert store.get_state("bias_calibration_decayed") == "false"


def test_recompute_does_not_decay_a_helping_bias():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.set_state("learning_half_life_days", "0")
        for d in range(1, 10):  # steady 1.5× underestimate — the bias helps
            store.log_episode(
                "task", predicted_value=10, actual_value=15, timestamp=f"2026-01-{d:02d} 09:00:00"
            )
        summary = recompute_patterns(store)

        assert summary.calibration.helps is True
        assert summary.bias_pre_decay is None
        assert summary.bias == 1.5
        assert store.get_state("time_estimation_bias") == "1.5"
        assert store.get_state("bias_calibration_decayed") == "false"


# -- context-conditioned bias by time of day (learning §5) -------------------


def _at_hour(pred, act, hour):
    return _ep(predicted_value=pred, actual_value=act, timestamp=f"2026-01-05 {hour:02d}:00:00")


def test_time_of_day_band_buckets():
    assert time_of_day_band(6) == "morning"
    assert time_of_day_band(13) == "afternoon"
    assert time_of_day_band(20) == "evening"
    assert time_of_day_band(2) == "evening"  # overnight tail folds into evening


def test_compute_bias_by_band_conditions_on_local_hour():
    episodes = (
        [_at_hour(10, 20, 9) for _ in range(3)]     # mornings run 2× long
        + [_at_hour(10, 10, 14) for _ in range(3)]  # afternoons on the nose
        + [_at_hour(10, 30, 20) for _ in range(2)]  # only 2 evenings — below the floor
    )
    banded = compute_bias_by_band(episodes, timezone="UTC")
    assert banded == {"morning": 2.0, "afternoon": 1.0}  # evening omitted (too few)


def test_resolve_bias_prefers_band_then_global():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        # No band learned yet → the (seeded) global multiplier.
        assert resolve_bias(store, local_hour=9) == 1.4
        store.set_state("time_estimation_bias", "1.4")
        store.set_state("time_estimation_bias:morning", "2.0")
        assert resolve_bias(store, local_hour=9) == 2.0    # band wins
        assert resolve_bias(store, local_hour=14) == 1.4   # no afternoon band → global
        assert resolve_bias(store) == 1.4                  # no hour → global


def test_recompute_persists_band_bias():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for _ in range(3):
            store.log_episode(
                "task", predicted_value=10, actual_value=20,
                timestamp="2026-01-05 09:00:00",
            )
        summary = recompute_patterns(store, timezone="UTC")
        assert summary.band_bias.get("morning") == 2.0
        assert store.get_state("time_estimation_bias:morning") == "2.0"


# -- context-conditioned bias by task type (learning §5) ---------------------


def test_compute_bias_by_type_conditions_on_episode_type():
    episodes = (
        [_ep(episode_type="task", predicted_value=10, actual_value=20) for _ in range(3)]
        + [_ep(episode_type="departure", predicted_value=10, actual_value=10) for _ in range(3)]
        + [_ep(episode_type="mail", predicted_value=10, actual_value=30) for _ in range(2)]
    )
    typed = compute_bias_by_type(episodes)
    # task runs 2× long, departures on the nose; mail has only 2 pairs → omitted.
    assert typed == {"task": 2.0, "departure": 1.0}


def test_compute_bias_by_type_ignores_pairs_without_actuals():
    # Todo closes log a "task" episode with actual_value=None — they must not
    # feed the type bias (created→closed is wall-clock, not time on task).
    episodes = [_ep(episode_type="task", predicted_value=10, actual_value=None) for _ in range(5)]
    assert compute_bias_by_type(episodes) == {}


def test_resolve_bias_prefers_band_then_type_then_global():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.set_state("time_estimation_bias", "1.4")
        store.set_state(f"{TYPE_BIAS_PREFIX}task", "1.7")
        # No hour, no type → global.
        assert resolve_bias(store) == 1.4
        # Type only → the type multiplier.
        assert resolve_bias(store, episode_type="task") == 1.7
        # Unknown type → global.
        assert resolve_bias(store, episode_type="mail") == 1.4
        # Band beats type when both are learned and the hour has a band.
        store.set_state("time_estimation_bias:morning", "2.0")
        assert resolve_bias(store, local_hour=9, episode_type="task") == 2.0
        # No band for the hour → falls through to the type bias, not straight to global.
        assert resolve_bias(store, local_hour=14, episode_type="task") == 1.7


def test_recompute_persists_type_bias():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for _ in range(3):
            store.log_episode("task", predicted_value=10, actual_value=20)
        summary = recompute_patterns(store, timezone="UTC")
        assert summary.type_bias.get("task") == 2.0
        assert store.get_state(f"{TYPE_BIAS_PREFIX}task") == "2.0"


# -- context-conditioned bias by energy / category (learning §5) -------------


def test_compute_bias_by_energy_and_category_condition_on_tags():
    episodes = (
        [_ep(predicted_value=10, actual_value=20, energy="high", category="creative")
         for _ in range(3)]
        + [_ep(predicted_value=10, actual_value=10, energy="low", category="admin")
           for _ in range(3)]
        + [_ep(predicted_value=10, actual_value=30, energy="medium") for _ in range(2)]  # too few
    )
    assert compute_bias_by_energy(episodes) == {"high": 2.0, "low": 1.0}
    assert compute_bias_by_category(episodes) == {"creative": 2.0, "admin": 1.0}


def test_compute_bias_by_energy_normalizes_and_skips_untagged():
    episodes = (
        [_ep(predicted_value=10, actual_value=20, energy="  High ") for _ in range(3)]
        + [_ep(predicted_value=10, actual_value=15, energy=None) for _ in range(3)]  # untagged
    )
    # Case/space-folded to "high"; untagged episodes never form a bucket.
    assert compute_bias_by_energy(episodes) == {"high": 2.0}


# -- context-conditioned bias by activity (finer than episode type) ----------


def test_episode_activity_reads_the_context_prefix():
    # The leading word before any ":" or space, lowercased.
    assert episode_activity(_ep(context="outing: coffee run")) == "outing"
    assert episode_activity(_ep(context="outing abandoned: dentist")) == "outing"
    assert episode_activity(_ep(context="focus: deep work")) == "focus"
    assert episode_activity(_ep(context="trip")) == "trip"
    # No context → no activity (the episode simply doesn't condition this dimension).
    assert episode_activity(_ep(context=None)) is None
    assert episode_activity(_ep(context="")) is None


def test_compute_bias_by_activity_separates_outing_from_focus():
    """The whole point: outings and focus blocks both log type=task pairs, but this
    dimension buckets them apart so a coffee run calibrates against outing history."""
    episodes = (
        # Outings run 3× their stated window (chronic "back in 15" → 45).
        [_ep(episode_type="task", context="outing: coffee", predicted_value=15, actual_value=45)
         for _ in range(3)]
        # Focus blocks are estimated on the nose.
        + [_ep(episode_type="task", context="focus: writing", predicted_value=30, actual_value=30)
           for _ in range(3)]
        # A trip return carries no prediction (actual-only) → never forms a pair.
        + [_ep(episode_type="task", context="trip", predicted_value=None, actual_value=25)
           for _ in range(4)]
    )
    activity = compute_bias_by_activity(episodes)
    assert activity == {"outing": 3.0, "focus": 1.0}
    # And note the pooled type bias blends them (Σactual/Σpredicted =
    # (135+90)/(45+90) = 1.67), landing on neither — which is exactly why the finer
    # bucket exists: a coffee run projected at 1.67× under-shoots its real 3×.
    assert compute_bias_by_type(episodes)["task"] == 1.67


def test_resolve_bias_activity_is_finer_than_type():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.set_state("time_estimation_bias", "1.4")
        store.set_state(f"{TYPE_BIAS_PREFIX}task", "2.0")
        store.set_state(f"{ACTIVITY_BIAS_PREFIX}outing", "3.0")
        # No context → global.
        assert resolve_bias(store) == 1.4
        # Type only → the pooled task multiplier.
        assert resolve_bias(store, episode_type="task") == 2.0
        # Activity beats type: an outing prefers its own history.
        assert resolve_bias(store, episode_type="task", activity="outing") == 3.0
        # Unknown activity → falls through to the type bias, not straight to global.
        assert resolve_bias(store, episode_type="task", activity="trip") == 2.0
        # Activity with no type and no learned activity bucket → global.
        assert resolve_bias(store, activity="errand") == 1.4


def test_recompute_persists_activity_bias():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for _ in range(3):
            store.log_episode(
                "task", context="outing: coffee", predicted_value=15, actual_value=45
            )
        summary = recompute_patterns(store, timezone="UTC")
        assert summary.activity_bias.get("outing") == 3.0
        assert store.get_state(f"{ACTIVITY_BIAS_PREFIX}outing") == "3.0"


def test_resolve_bias_full_precedence_band_energy_category_type_global():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.set_state("time_estimation_bias", "1.4")
        store.set_state(f"{TYPE_BIAS_PREFIX}task", "1.6")
        store.set_state(f"{CATEGORY_BIAS_PREFIX}admin", "1.7")
        store.set_state(f"{ENERGY_BIAS_PREFIX}high", "1.8")
        store.set_state("time_estimation_bias:morning", "2.0")
        # All supplied → band wins (finest, shipped-first).
        assert resolve_bias(
            store, local_hour=9, episode_type="task", energy="high", category="admin"
        ) == 2.0
        # No band for the hour → energy is next.
        assert resolve_bias(
            store, local_hour=14, episode_type="task", energy="high", category="admin"
        ) == 1.8
        # No energy learned for this load → category.
        assert resolve_bias(
            store, local_hour=14, episode_type="task", energy="low", category="admin"
        ) == 1.7
        # Neither energy nor category learned → the task-type bias.
        assert resolve_bias(
            store, local_hour=14, episode_type="task", energy="low", category="chores"
        ) == 1.6
        # Nothing conditioned → global.
        assert resolve_bias(store, energy="low", category="chores") == 1.4


def test_task_bias_resolver_uses_todo_energy_and_category():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.set_state("time_estimation_bias", "1.4")
        store.set_state(f"{ENERGY_BIAS_PREFIX}high", "1.9")
        resolve = task_bias_resolver(store, local_hour=14)  # afternoon, no band learned
        assert resolve({"energy": "high", "category": "x"}) == 1.9   # energy hit
        assert resolve({"energy": "low", "category": "x"}) == 1.4     # falls to global


def test_recompute_persists_energy_and_category_bias():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for _ in range(3):
            store.log_episode(
                "task", predicted_value=10, actual_value=20, energy="high", category="creative"
            )
        summary = recompute_patterns(store, timezone="UTC")
        assert summary.energy_bias.get("high") == 2.0
        assert summary.category_bias.get("creative") == 2.0
        assert store.get_state(f"{ENERGY_BIAS_PREFIX}high") == "2.0"
        assert store.get_state(f"{CATEGORY_BIAS_PREFIX}creative") == "2.0"


# -- learning bucketing: sliding window + per-context half-lives (§3) ---------


def test_within_window():
    now = datetime(2026, 2, 1, 0, 0, 0)
    assert within_window("2026-01-01 00:00:00", now, 0) is True  # disabled → keep
    assert within_window("2020-01-01 00:00:00", now, None) is True  # disabled → keep
    assert within_window("2026-01-20 00:00:00", now, 30) is True  # ~12d old, inside
    assert within_window("2025-01-01 00:00:00", now, 30) is False  # ~1yr old, outside
    assert within_window(None, now, 30) is True  # missing ts kept (forgiving)
    assert within_window("2026-03-01 00:00:00", now, 30) is True  # future (skew) kept


def test_filter_to_window():
    now = datetime(2026, 2, 1, 0, 0, 0)
    eps = [
        _ep(timestamp="2020-01-01 00:00:00"),  # ancient
        _ep(timestamp="2026-01-25 00:00:00"),  # recent
    ]
    assert filter_to_window(eps, now, 0) == eps  # disabled → identity (same object)
    kept = filter_to_window(eps, now, 30)
    assert len(kept) == 1 and kept[0]["timestamp"] == "2026-01-25 00:00:00"


def test_compute_bias_by_type_honors_per_context_half_life():
    """A per-context half-life overrides the global decay for just that bucket."""
    now = datetime(2026, 2, 1, 0, 0, 0)
    episodes = [
        _ep(episode_type="focus", predicted_value=10, actual_value=20,
            timestamp="2025-08-01 00:00:00"),  # old 2× overrun
        _ep(episode_type="focus", predicted_value=10, actual_value=10,
            timestamp="2026-02-01 00:00:00"),  # recent on time
        _ep(episode_type="focus", predicted_value=10, actual_value=10,
            timestamp="2026-02-01 00:00:00"),  # recent on time
    ]
    # Global equal-weight (half_life_days=0): the old overrun counts fully.
    flat = compute_bias_by_type(episodes, now=now, half_life_days=0)["focus"]
    assert flat == pytest.approx(1.33, abs=0.01)  # (20+10+10)/30
    # A 30d half-life for type:focus (and nothing else) discounts the old overrun.
    resolver = lambda suffix: 30.0 if suffix == "type:focus" else None  # noqa: E731
    tuned = compute_bias_by_type(
        episodes, now=now, half_life_days=0, half_life_for=resolver
    )["focus"]
    assert tuned < flat and tuned == pytest.approx(1.0, abs=0.05)


def test_make_half_life_resolver_reads_override_keys():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.set_state("learning_half_life_days:type:focus", "7", source="explicit")
        store.set_state("learning_half_life_days:morning", "0", source="explicit")
        store.set_state("learning_half_life_days:bad", "notanumber", source="explicit")
        resolve = _make_half_life_resolver(store)
        assert resolve("type:focus") == 7.0
        assert resolve("morning") == 0.0  # explicit 0 honored (no decay for this context)
        assert resolve("type:admin") is None  # unset → fall back to global
        assert resolve("bad") is None  # unparseable → treated as unset


def test_learning_window_excludes_stale_episodes_end_to_end():
    """The learning_window_days key drops old episodes before anything is learned."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        # Ancient overruns that would drag the global bias up...
        for _ in range(4):
            store.log_episode(
                "task", predicted_value=10, actual_value=20, timestamp="2020-01-01 00:00:00"
            )
        # ...and recent on-time pairs (default timestamp = now).
        for _ in range(4):
            store.log_episode("task", predicted_value=10, actual_value=10)
        store.set_state("learning_half_life_days", "0")  # isolate the window from decay
        store.set_state("bias_decay_on_miss", "0")  # report-only, so no §4 auto-act interferes

        # No window: all 8 pairs count → (4·20 + 4·10) / (8·10) = 1.5.
        recompute_patterns(store)
        assert float(store.get_state("time_estimation_bias")) == pytest.approx(1.5, abs=0.01)

        # A 365d window drops the 2020 pairs → only recent on-time remain → 1.0.
        store.set_state("learning_window_days", "365")
        summary = recompute_patterns(store)
        assert float(store.get_state("time_estimation_bias")) == pytest.approx(1.0, abs=0.01)
        assert summary.window_days == 365.0
        assert summary.windowed_out == 4
        assert summary.episodes == 4


def test_per_context_half_life_end_to_end_leaves_others_on_global():
    """A per-context override changes only its bucket; other buckets keep the global."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        # focus: an old overrun + two recent on-time; admin: same shape.
        for etype in ("focus", "admin"):
            store.log_episode(
                etype, predicted_value=10, actual_value=20, timestamp="2020-01-01 00:00:00"
            )
            store.log_episode(etype, predicted_value=10, actual_value=10)
            store.log_episode(etype, predicted_value=10, actual_value=10)
        store.set_state("learning_half_life_days", "0")  # global: equal weight
        # Override only focus with a short (real) half-life so its ancient overrun decays.
        store.set_state("learning_half_life_days:type:focus", "30", source="explicit")

        summary = recompute_patterns(store)
        # focus discounts the old overrun (→ ~1.0); admin keeps equal weight (→ 1.33).
        assert summary.type_bias["focus"] == pytest.approx(1.0, abs=0.05)
        assert summary.type_bias["admin"] == pytest.approx(1.33, abs=0.01)


# -- auto-derived per-context half-lives (§ learning: adaptive decay) ---------


def _pa(pred, act, day):
    """A predicted/actual episode at 09:00 (morning band) on 2026-01-<day>."""
    return _ep(predicted_value=pred, actual_value=act, timestamp=f"2026-01-{day:02d} 09:00:00")


def test_derive_half_life_stable_context_decays_slower():
    """A context whose bias holds steady trusts more history → up to 2× the global."""
    eps = [_pa(10, 14, d) for d in range(1, 9)]  # 8 pairs, constant 1.4 bias
    assert derive_half_life(eps, 30.0, now=datetime(2026, 2, 1)) == 60.0


def test_derive_half_life_churny_context_decays_faster():
    """A context whose bias has drifted follows the change → down to 0.5× the global."""
    older = [_pa(10, 10, d) for d in range(1, 5)]   # bias 1.0
    recent = [_pa(10, 20, d) for d in range(5, 9)]  # bias 2.0 (big drift)
    assert derive_half_life(older + recent, 30.0, now=datetime(2026, 2, 1)) == 15.0


def test_derive_half_life_guards():
    """Too few pairs, or decay disabled, yields no suggestion."""
    assert derive_half_life([_pa(10, 10, d) for d in range(1, 4)], 30.0) is None  # < 2×floor
    assert derive_half_life([_pa(10, 10, d) for d in range(1, 9)], 0) is None      # decay off


def test_derive_band_half_lives_buckets_by_time_of_day():
    """Each band's half-life is derived from that band's own episodes."""
    morning = [_ep(predicted_value=10, actual_value=14, timestamp=f"2026-01-{d:02d} 09:00:00")
               for d in range(1, 9)]  # stable → 2× global
    evening = [_ep(predicted_value=10, actual_value=10, timestamp=f"2026-01-{d:02d} 20:00:00")
               for d in range(1, 5)]
    evening += [_ep(predicted_value=10, actual_value=20, timestamp=f"2026-01-{d:02d} 20:00:00")
                for d in range(5, 9)]  # churn → 0.5× global
    out = derive_band_half_lives(morning + evening, 30.0, now=datetime(2026, 2, 1), timezone="UTC")
    assert out["morning"] == 60.0 and out["evening"] == 15.0


def test_derive_context_half_lives_covers_type_energy_category():
    """Auto-derivation spans every context dimension, keyed by its suffix."""
    def ep(pred, act, day, **tags):
        return _ep(predicted_value=pred, actual_value=act,
                   timestamp=f"2026-01-{day:02d} 09:00:00", **tags)
    eps = [ep(10, 10, d, energy="high", category="admin") for d in range(1, 5)]
    eps += [ep(10, 20, d, energy="high", category="admin") for d in range(5, 9)]  # churn
    out = derive_context_half_lives(eps, 30.0, now=datetime(2026, 2, 1), timezone="UTC")
    assert out["morning"] == 15.0        # time-of-day band
    assert out["type:task"] == 15.0      # episode type
    assert out["energy:high"] == 15.0    # energy tag
    assert out["category:admin"] == 15.0  # category tag


def test_recompute_auto_half_life_writes_all_dimension_keys():
    """The learn pass persists the derived half-life for each context dimension."""
    with MemoryStore.open(":memory:") as s:
        store = scoped_default(s)
        store.set_state("auto_context_half_life", "on")
        for d in range(1, 5):
            store.log_episode("task", predicted_value=10, actual_value=10,
                              energy="high", category="admin",
                              timestamp=f"2026-01-{d:02d} 09:00:00")
        for d in range(5, 9):
            store.log_episode("task", predicted_value=10, actual_value=20,
                              energy="high", category="admin",
                              timestamp=f"2026-01-{d:02d} 09:00:00")
        summary = recompute_patterns(store, timezone="UTC")
        assert summary.auto_half_lives["category:admin"] == 15.0
        assert store.get_state("learning_half_life_days:category:admin") == "15.0"
        assert store.get_state("learning_half_life_days:type:task") == "15.0"
        assert store.get_state("learning_half_life_days:energy:high") == "15.0"


def test_recompute_auto_half_life_writes_band_keys_when_on():
    """With the toggle on, a churny morning band gets a shortened half-life written."""
    with MemoryStore.open(":memory:") as s:
        store = scoped_default(s)
        store.set_state("auto_context_half_life", "on")
        for d in range(1, 5):  # older: on-time
            store.log_episode("task", predicted_value=10, actual_value=10,
                              timestamp=f"2026-01-{d:02d} 09:00:00")
        for d in range(5, 9):  # recent: 2× overrun → churn
            store.log_episode("task", predicted_value=10, actual_value=20,
                              timestamp=f"2026-01-{d:02d} 09:00:00")
        summary = recompute_patterns(store, timezone="UTC")
        assert summary.auto_half_lives.get("morning") == 15.0
        assert store.get_state("learning_half_life_days:morning") == "15.0"


def test_recompute_auto_half_life_off_by_default_and_respects_explicit():
    """Off unless toggled; and a hand-set band half-life is never overwritten."""
    with MemoryStore.open(":memory:") as s:
        store = scoped_default(s)
        for d in range(1, 9):
            store.log_episode("task", predicted_value=10, actual_value=20,
                              timestamp=f"2026-01-{d:02d} 09:00:00")
        assert recompute_patterns(store, timezone="UTC").auto_half_lives == {}
        assert not store.get_state("learning_half_life_days:morning")

        store.set_state("auto_context_half_life", "on")
        store.set_state("learning_half_life_days:morning", "45", source="explicit")
        summary = recompute_patterns(store, timezone="UTC")
        assert "morning" not in summary.auto_half_lives
        assert store.get_state("learning_half_life_days:morning") == "45"


# -- channel-choice walk-forward calibration (§4) ----------------------------


def test_channel_calibration_helps_when_channel_predicts_ack():
    """When one channel is reliably acked and another ignored, conditioning wins."""
    eps = []
    for d in range(1, 6):
        eps.append(_ep(channel="push", acknowledged=True, timestamp=f"2026-01-{d:02d} 09:00:00"))
        eps.append(_ep(channel="sms", acknowledged=False, timestamp=f"2026-01-{d:02d} 10:00:00"))
    cal = channel_calibration(eps)
    assert cal.status == "ok"
    assert cal.helps and cal.adjusted_error < cal.baseline_error


def test_channel_calibration_insufficient_without_enough_deliveries():
    assert channel_calibration(
        [_ep(channel="push", acknowledged=True)]
    ).status == "insufficient"


def test_recompute_persists_channel_calibration():
    """The learn pass runs the channel walk-forward and persists its verdict."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for d in range(1, 6):
            store.log_episode("reminder", channel="push", acknowledged=True,
                              timestamp=f"2026-01-{d:02d} 09:00:00")
            store.log_episode("reminder", channel="sms", acknowledged=False,
                              timestamp=f"2026-01-{d:02d} 10:00:00")
        summary = recompute_patterns(store)
        assert summary.channel_calibration.status == "ok"
        assert summary.channel_calibration.helps
        assert store.get_state("channel_calibration_helps") == "true"
        assert store.get_state("channel_calibration_samples") is not None


# -- channel-choice auto-act (§4): damp a non-predictive channel signal ------


def test_pooled_channel_rate_is_sample_weighted():
    pats = [
        {"observed_value": 1.0, "sample_size": 3},
        {"observed_value": 0.0, "sample_size": 1},
    ]
    assert pooled_channel_rate(pats) == 0.75  # (1.0*3 + 0.0*1) / 4
    assert pooled_channel_rate([]) is None
    assert pooled_channel_rate([{"observed_value": None, "sample_size": 5}]) is None


def test_decay_channel_rate_toward_pooled():
    assert decay_channel_rate_toward_pooled(1.0, 0.5, 0.5) == 0.75  # halve the deviation
    assert decay_channel_rate_toward_pooled(0.0, 0.5, 1.0) == 0.5   # flatten fully
    assert decay_channel_rate_toward_pooled(0.8, 0.5, 0.0) == 0.8   # disabled → unchanged
    # A stray >1 factor still only moves toward pooled (clamped), never past it.
    assert decay_channel_rate_toward_pooled(1.0, 0.5, 5.0) == 0.5


def _seed_nonpredictive_channels(store):
    """Deliveries whose per-channel ack-rate flips between train and test slices.

    Older (train): push acked, sms ignored. Newer (test, the recent third): the
    reverse — so conditioning on channel predicts held-out acks *worse* than a
    pooled rate, i.e. the channel signal is noise (helps=False).
    """
    for d in range(1, 5):  # 8 train deliveries
        store.log_episode("reminder", channel="push", acknowledged=True,
                          timestamp=f"2026-06-{d:02d} 09:00:00")
        store.log_episode("reminder", channel="sms", acknowledged=False,
                          timestamp=f"2026-06-{d:02d} 10:00:00")
    for d in range(10, 12):  # 4 test deliveries, flipped
        store.log_episode("reminder", channel="push", acknowledged=False,
                          timestamp=f"2026-06-{d:02d} 09:00:00")
        store.log_episode("reminder", channel="sms", acknowledged=True,
                          timestamp=f"2026-06-{d:02d} 10:00:00")


def _channel_rates(store):
    return {
        p["context_key"]: p["observed_value"]
        for p in store.get_patterns("channel_response")
    }


def test_channel_autoact_damps_nonpredictive_rates():
    """A 'not helping' channel verdict flattens the per-channel rates toward pooled."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        _seed_nonpredictive_channels(store)
        summary = recompute_patterns(store)
        assert summary.channel_calibration.helps is False
        assert summary.channel_decayed is True
        assert store.get_state("channel_calibration_decayed") == "true"
        damped = _channel_rates(store)

    # Same episodes, auto-act disabled (report-only) → rates left as computed.
    with MemoryStore.open(":memory:") as raw:
        store2 = scoped_default(raw)
        _seed_nonpredictive_channels(store2)
        store2.set_state("channel_decay_on_miss", "0")
        s2 = recompute_patterns(store2)
        assert s2.channel_calibration.helps is False
        assert s2.channel_decayed is False
        assert store2.get_state("channel_calibration_decayed") == "false"
        raw_rates = _channel_rates(store2)

    # Damping strictly shrinks the spread between channels (toward the pooled rate),
    # so choose_channel stops bumping on the noise.
    assert abs(damped["push"] - damped["sms"]) < abs(raw_rates["push"] - raw_rates["sms"])


def test_channel_autoact_noop_when_channel_helps():
    """A predictive channel signal is left intact — nothing to correct."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for d in range(1, 6):
            store.log_episode("reminder", channel="push", acknowledged=True,
                              timestamp=f"2026-06-{d:02d} 09:00:00")
            store.log_episode("reminder", channel="sms", acknowledged=False,
                              timestamp=f"2026-06-{d:02d} 10:00:00")
        s = recompute_patterns(store)
        assert s.channel_calibration.helps is True
        assert s.channel_decayed is False
        assert store.get_state("channel_calibration_decayed") == "false"


# -- early-start threshold learning (Time Blindness morning_prep) -------------


def _late_departure(hhmm, day=5):
    """A late (missed) departure episode at a given local clock time."""
    return _ep(episode_type="departure", outcome="miss", timestamp=f"2026-01-{day:02d} {hhmm}:00")


def test_early_start_threshold_learns_from_late_morning_departures():
    """The cutoff = recency-weighted mean late-departure time + the 30-min margin."""
    eps = [_late_departure("07:00", d) for d in range(1, 5)]  # four late 7am departures
    assert compute_early_start_threshold(eps, timezone="UTC", half_life_days=None) == "07:30"


def test_early_start_threshold_none_below_sample_floor():
    eps = [_late_departure("07:00", d) for d in range(1, 4)]  # only three
    assert compute_early_start_threshold(eps, timezone="UTC") is None


def test_early_start_threshold_ignores_on_time_and_non_morning():
    """Only *late* *morning* departures inform the cutoff."""
    on_time = [
        _ep(episode_type="departure", outcome="success", timestamp=f"2026-01-{d:02d} 07:00:00")
        for d in range(1, 5)
    ]
    assert compute_early_start_threshold(on_time, timezone="UTC") is None
    afternoon = [_late_departure("14:00", d) for d in range(1, 5)]
    assert compute_early_start_threshold(afternoon, timezone="UTC") is None


def test_early_start_threshold_is_clamped():
    late = [_late_departure("09:55", d) for d in range(1, 5)]  # +30 → 10:25, clamp → 10:00
    assert compute_early_start_threshold(late, timezone="UTC", half_life_days=None) == "10:00"


def test_recompute_learns_and_persists_early_start_threshold():
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        for d in range(1, 5):
            store.log_episode("departure", outcome="miss", timestamp=f"2026-01-{d:02d} 07:00:00")
        summary = recompute_patterns(store, timezone="UTC")
        assert summary.early_start_threshold == "07:30"
        assert store.get_state("early_start_threshold") == "07:30"


def test_recompute_respects_explicit_early_start_threshold():
    """A hand-set cutoff is never overwritten by the learner (like the half-lives)."""
    with MemoryStore.open(":memory:") as raw:
        store = scoped_default(raw)
        store.set_state("early_start_threshold", "08:30", source="explicit")
        for d in range(1, 5):
            store.log_episode("departure", outcome="miss", timestamp=f"2026-01-{d:02d} 07:00:00")
        recompute_patterns(store, timezone="UTC")
        assert store.get_state("early_start_threshold") == "08:30"
