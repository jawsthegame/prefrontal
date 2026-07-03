"""Tests for the pattern-computation pass (the learning engine).

Covers the pure compute_patterns / compute_bias functions with synthetic
episodes, the confidence estimator, and the end-to-end recompute_patterns against
an in-memory store (including idempotency and bias write-back).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from prefrontal.memory.patterns import (
    compute_bias,
    compute_bias_by_band,
    compute_confidence,
    compute_patterns,
    decay_weight,
    recompute_patterns,
    resolve_bias,
    time_of_day_band,
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
