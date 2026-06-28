"""The pattern-computation pass — Prefrontal's learning engine.

This is the step that turns raw ``episodes`` into derived ``patterns``: the
calibrated, confidence-weighted summaries the coaching layer reasons over and the
behavioral profile is built from. It is the mechanism behind "it gets better the
longer you use it" — every recompute folds new outcomes into the estimates.

It derives the three pattern types we currently have source data for:

- **time_estimation** — predicted vs actual durations, grouped by episode type.
  ``variance`` is ``observed - predicted`` (positive ⇒ chronic underestimate),
  matching ``docs/schema.md``. Also recomputes the global ``time_estimation_bias``
  coaching multiplier (Σactual / Σpredicted), so a "miss" outing sharpens future
  estimates.
- **channel_response** — acknowledgement rate per delivery channel (which nudges
  actually land).
- **drift** — how often an episode type goes off-track, scored
  ``success=0, partial=0.5, miss=1.0``.

``context_switch`` from the schema needs switch-event source data Prefrontal does
not yet capture, so it is not computed here (see ``ROADMAP.md``).

The computation is split into a **pure** :func:`compute_patterns` (list of
episode dicts in, list of :class:`PatternResult` out — trivially testable) and
:func:`recompute_patterns`, which wires it to a store and persists the results.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any

from prefrontal.memory.store import MemoryStore

#: Smoothing constant for the confidence estimator ``n / (n + k)``. With k=5,
#: 5 samples ⇒ 0.5, 15 ⇒ 0.75, 45 ⇒ 0.9 — "low until the sample is meaningful".
DEFAULT_CONFIDENCE_K = 5.0

#: Minimum predicted/actual pairs before we trust a recomputed bias multiplier.
MIN_BIAS_SAMPLES = 3

#: How much each outcome contributes to a drift score (higher = more off-track).
DRIFT_WEIGHTS = {"success": 0.0, "partial": 0.5, "miss": 1.0}


@dataclass(frozen=True)
class PatternResult:
    """One derived pattern, ready to upsert into the ``patterns`` table."""

    pattern_type: str
    context_key: str
    sample_size: int
    confidence: float
    observed_value: float | None = None
    predicted_value: float | None = None
    variance: float | None = None


@dataclass(frozen=True)
class PatternRunSummary:
    """Summary of a single :func:`recompute_patterns` run."""

    episodes: int
    patterns: int
    by_type: dict[str, int] = field(default_factory=dict)
    bias: float | None = None


def compute_confidence(n: int, k: float = DEFAULT_CONFIDENCE_K) -> float:
    """Return a 0–1 confidence that grows with sample size.

    Uses ``n / (n + k)``: zero at no data, rising and asymptotically approaching
    1.0 as samples accumulate. ``k`` sets how fast trust builds.

    Args:
        n: Number of contributing samples.
        k: Smoothing constant (larger ⇒ slower to trust).

    Returns:
        Confidence in ``[0.0, 1.0)``.
    """
    if n <= 0:
        return 0.0
    return n / (n + k)


def compute_patterns(
    episodes: list[dict[str, Any]], *, confidence_k: float = DEFAULT_CONFIDENCE_K
) -> list[PatternResult]:
    """Derive all pattern rows from a list of episodes (pure function).

    Args:
        episodes: Episode dicts (as returned by the store).
        confidence_k: Smoothing constant passed to :func:`compute_confidence`.

    Returns:
        A list of :class:`PatternResult`, one per ``(pattern_type, context_key)``
        group that has at least one contributing episode.
    """
    results: list[PatternResult] = []
    results += _time_estimation_patterns(episodes, confidence_k)
    results += _channel_response_patterns(episodes, confidence_k)
    results += _drift_patterns(episodes, confidence_k)
    return results


def compute_bias(episodes: list[dict[str, Any]]) -> float | None:
    """Recompute the global time-estimation bias multiplier (Σactual / Σpredicted).

    Args:
        episodes: Episode dicts.

    Returns:
        The multiplier rounded to 2 dp (e.g. ``1.4`` ⇒ 40% underestimate), or
        ``None`` if there aren't enough usable pairs to trust it.
    """
    pairs = [
        (e["predicted_value"], e["actual_value"])
        for e in episodes
        if e.get("predicted_value") and e.get("actual_value") is not None
    ]
    if len(pairs) < MIN_BIAS_SAMPLES:
        return None
    total_pred = sum(p for p, _ in pairs)
    total_act = sum(a for _, a in pairs)
    if total_pred <= 0:
        return None
    return round(total_act / total_pred, 2)


def recompute_patterns(
    store: MemoryStore,
    *,
    confidence_k: float = DEFAULT_CONFIDENCE_K,
    update_bias: bool = True,
) -> PatternRunSummary:
    """Recompute every pattern from the full episode history and persist it.

    Idempotent: patterns are upserted by ``(pattern_type, context_key)``, so
    running it repeatedly on the same data converges rather than duplicating.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        confidence_k: Smoothing constant for confidence.
        update_bias: When ``True``, also refresh the ``time_estimation_bias``
            coaching value if there is enough data.

    Returns:
        A :class:`PatternRunSummary` describing what was written.
    """
    episodes = store.all_episodes()
    results = compute_patterns(episodes, confidence_k=confidence_k)
    for r in results:
        store.upsert_pattern(
            r.pattern_type,
            r.context_key,
            observed_value=r.observed_value,
            predicted_value=r.predicted_value,
            variance=r.variance,
            sample_size=r.sample_size,
            confidence=r.confidence,
        )

    bias: float | None = None
    if update_bias:
        bias = compute_bias(episodes)
        if bias is not None:
            store.set_state("time_estimation_bias", str(bias), source="inferred")

    return PatternRunSummary(
        episodes=len(episodes),
        patterns=len(results),
        by_type=dict(Counter(r.pattern_type for r in results)),
        bias=bias,
    )


# -- per-type helpers --------------------------------------------------------


def _time_estimation_patterns(
    episodes: list[dict[str, Any]], k: float
) -> list[PatternResult]:
    """Mean predicted/actual and variance per episode type."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if e.get("predicted_value") is not None and e.get("actual_value") is not None:
            groups[e["episode_type"]].append(e)

    results: list[PatternResult] = []
    for context_key, eps in groups.items():
        observed = fmean(e["actual_value"] for e in eps)
        predicted = fmean(e["predicted_value"] for e in eps)
        results.append(
            PatternResult(
                pattern_type="time_estimation",
                context_key=context_key,
                observed_value=round(observed, 2),
                predicted_value=round(predicted, 2),
                variance=round(observed - predicted, 2),
                sample_size=len(eps),
                confidence=round(compute_confidence(len(eps), k), 3),
            )
        )
    return results


def _channel_response_patterns(
    episodes: list[dict[str, Any]], k: float
) -> list[PatternResult]:
    """Acknowledgement rate per delivery channel."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if e.get("channel") and e.get("acknowledged") is not None:
            groups[e["channel"]].append(e)

    results: list[PatternResult] = []
    for context_key, eps in groups.items():
        ack_rate = fmean(1.0 if e["acknowledged"] else 0.0 for e in eps)
        results.append(
            PatternResult(
                pattern_type="channel_response",
                context_key=context_key,
                observed_value=round(ack_rate, 3),
                sample_size=len(eps),
                confidence=round(compute_confidence(len(eps), k), 3),
            )
        )
    return results


def _drift_patterns(episodes: list[dict[str, Any]], k: float) -> list[PatternResult]:
    """Off-track score per episode type from outcomes."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if e.get("outcome") in DRIFT_WEIGHTS:
            groups[e["episode_type"]].append(e)

    results: list[PatternResult] = []
    for context_key, eps in groups.items():
        score = fmean(DRIFT_WEIGHTS[e["outcome"]] for e in eps)
        results.append(
            PatternResult(
                pattern_type="drift",
                context_key=context_key,
                observed_value=round(score, 3),
                sample_size=len(eps),
                confidence=round(compute_confidence(len(eps), k), 3),
            )
        )
    return results
