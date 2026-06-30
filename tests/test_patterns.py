"""Tests for the pattern-computation pass (the learning engine).

Covers the pure compute_patterns / compute_bias functions with synthetic
episodes, the confidence estimator, and the end-to-end recompute_patterns against
an in-memory store (including idempotency and bias write-back).
"""

from __future__ import annotations

import pytest

from prefrontal.memory.patterns import (
    compute_bias,
    compute_confidence,
    compute_patterns,
    recompute_patterns,
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
