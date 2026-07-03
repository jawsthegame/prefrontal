"""The pattern-computation pass — Prefrontal's learning engine.

This is the step that turns raw ``episodes`` into derived ``patterns``: the
calibrated, confidence-weighted summaries the coaching layer reasons over and the
behavioral profile is built from. It is the mechanism behind "it gets better the
longer you use it" — every recompute folds new outcomes into the estimates.

Estimates are **recency-weighted**: each episode contributes by an exponential
decay on its age (:func:`decay_weight`, half-life ``learning_half_life_days``,
default 30d), so the profile *follows* a person whose behavior has changed rather
than being anchored to their cumulative average. Confidence uses the *effective*
sample size (the summed weights), so a group of only-stale episodes is trusted
less than the same count of fresh ones. Set the half-life to ``0`` to weigh all
history equally (the pre-decay behavior).

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
from datetime import datetime
from datetime import timezone as _timezone
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from prefrontal.impact import utcnow
from prefrontal.memory.store import MemoryStore

#: Smoothing constant for the confidence estimator ``n / (n + k)``. With k=5,
#: 5 samples ⇒ 0.5, 15 ⇒ 0.75, 45 ⇒ 0.9 — "low until the sample is meaningful".
DEFAULT_CONFIDENCE_K = 5.0

#: Minimum predicted/actual pairs before we trust a recomputed bias multiplier.
MIN_BIAS_SAMPLES = 3

#: Minimum pairs before we attempt a walk-forward calibration check (needs enough
#: to split into a train slice that meets ``MIN_BIAS_SAMPLES`` and a test slice).
MIN_CALIBRATION_SAMPLES = 6

#: Fraction of (time-ordered) pairs held out as the "recent" test slice when
#: checking whether the learned bias actually improved subsequent estimates.
DEFAULT_CALIBRATION_TEST_FRACTION = 0.34

#: How much each outcome contributes to a drift score (higher = more off-track).
DRIFT_WEIGHTS = {"success": 0.0, "partial": 0.5, "miss": 1.0}

#: Recency half-life (days) for the learning pass: an episode this old counts
#: half as much as a fresh one, and the weight halves again every half-life after
#: that (exponential decay). This is what lets the profile *follow* a person who
#: has changed rather than being anchored to their cumulative history. Tunable per
#: user via the ``learning_half_life_days`` coaching key; set it to ``0`` to
#: disable decay and weigh all history equally (the pre-decay behavior).
DEFAULT_HALF_LIFE_DAYS = 30.0


#: Time-of-day bands for context-conditioned learning (learning §5). The learned
#: time-estimation bias is computed per band as well as globally, because the same
#: person often mis-estimates very differently across the day (morning admin vs
#: afternoon deep work). ``resolve_bias`` prefers the band for the moment a
#: prediction is being made, falling back to the global multiplier.
TIME_OF_DAY_BANDS = ("morning", "afternoon", "evening")


def time_of_day_band(hour: int) -> str:
    """Map a local hour (0–23) to a coarse band (:data:`TIME_OF_DAY_BANDS`)."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    return "evening"  # 17:00–05:00 (evenings + the quiet overnight tail)


def _parse_ts(ts: Any) -> datetime | None:
    """Parse a stored episode timestamp (``YYYY-MM-DD HH:MM:SS``), else ``None``."""
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _local_hour(ts: Any, timezone: str) -> int | None:
    """Local wall-clock hour of a stored (naive-UTC) episode timestamp, or ``None``.

    Episodes are stored in UTC; the band a task *felt* like it happened in is the
    user's local hour, so we localize before bucketing. An unknown zone degrades
    to UTC rather than dropping the episode.
    """
    dt = _parse_ts(ts)
    if dt is None:
        return None
    try:
        zone = ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = _timezone.utc
    return dt.replace(tzinfo=_timezone.utc).astimezone(zone).hour


def decay_weight(ts: Any, now: datetime, half_life_days: float | None) -> float:
    """Exponential recency weight for an episode timestamp, in ``(0, 1]``.

    ``0.5 ** (age_days / half_life_days)`` — 1.0 for a just-now episode, 0.5 at
    one half-life, 0.25 at two, and so on. Returns ``1.0`` (no decay) when
    ``half_life_days`` is falsy/≤0, or when the timestamp is missing/unparseable
    (a missing time shouldn't silently zero out an episode).
    """
    if not half_life_days or half_life_days <= 0:
        return 1.0
    t = _parse_ts(ts)
    if t is None:
        return 1.0
    age_days = (now - t).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


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
class BiasCalibration:
    """Does the learned time-estimation bias actually improve estimates? (§4)

    A walk-forward check: learn a bias from the *older* pairs, then measure the
    mean absolute error of raw vs bias-adjusted predictions on the *newer* pairs
    it never saw. ``improvement`` (raw − adjusted, in the estimate's units, e.g.
    minutes) is positive when applying the bias got predictions closer to reality;
    ``helps`` is the verdict. This makes a bad adaptation *visible and
    self-correcting* rather than asserted, and hints when a pattern is worth
    trusting more assertively.
    """

    status: str  # "ok" | "insufficient"
    samples: int  # test-slice pairs the error was measured on
    train_bias: float | None = None
    raw_error: float | None = None
    adjusted_error: float | None = None
    improvement: float | None = None
    helps: bool = False


@dataclass(frozen=True)
class PatternRunSummary:
    """Summary of a single :func:`recompute_patterns` run."""

    episodes: int
    patterns: int
    by_type: dict[str, int] = field(default_factory=dict)
    bias: float | None = None
    calibration: BiasCalibration | None = None
    band_bias: dict[str, float] = field(default_factory=dict)


def compute_confidence(n: float, k: float = DEFAULT_CONFIDENCE_K) -> float:
    """Return a 0–1 confidence that grows with (effective) sample size.

    Uses ``n / (n + k)``: zero at no data, rising and asymptotically approaching
    1.0 as samples accumulate. ``k`` sets how fast trust builds. ``n`` may be a
    fractional *effective* sample size (the sum of recency weights), so a group
    of only-stale episodes trusts less than the same count of fresh ones.

    Args:
        n: Number of contributing samples (or the summed recency weight).
        k: Smoothing constant (larger ⇒ slower to trust).

    Returns:
        Confidence in ``[0.0, 1.0)``.
    """
    if n <= 0:
        return 0.0
    return n / (n + k)


def _wmean(pairs: list[tuple[float, float]]) -> float | None:
    """Weighted mean of ``(value, weight)`` pairs, or ``None`` if no weight."""
    total_w = sum(w for _, w in pairs)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in pairs) / total_w


def compute_patterns(
    episodes: list[dict[str, Any]],
    *,
    confidence_k: float = DEFAULT_CONFIDENCE_K,
    now: datetime | None = None,
    half_life_days: float | None = None,
) -> list[PatternResult]:
    """Derive all pattern rows from a list of episodes (pure function).

    Args:
        episodes: Episode dicts (as returned by the store).
        confidence_k: Smoothing constant passed to :func:`compute_confidence`.
        now: Reference instant for recency decay (defaults to :func:`utcnow`).
        half_life_days: Recency half-life; ``None``/``0`` weighs all episodes
            equally (no decay). :func:`recompute_patterns` supplies the per-user
            value, so the pure function stays decay-free by default for callers
            (and tests) that pass a bare episode list.

    Returns:
        A list of :class:`PatternResult`, one per ``(pattern_type, context_key)``
        group that has at least one contributing episode.
    """
    now = now or utcnow()
    results: list[PatternResult] = []
    results += _time_estimation_patterns(episodes, confidence_k, now, half_life_days)
    results += _channel_response_patterns(episodes, confidence_k, now, half_life_days)
    results += _drift_patterns(episodes, confidence_k, now, half_life_days)
    return results


def compute_bias(
    episodes: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
) -> float | None:
    """Recompute the global time-estimation bias multiplier (Σactual / Σpredicted).

    Recency-weighted: each pair contributes by its :func:`decay_weight`, so a
    recent run of on-time estimates pulls the multiplier back faster than a year
    of old overruns holds it up. The ``MIN_BIAS_SAMPLES`` floor still counts raw
    pairs, so decay changes the *weighting*, never whether we have enough data.

    Args:
        episodes: Episode dicts.
        now: Reference instant for decay (defaults to :func:`utcnow`).
        half_life_days: Recency half-life; ``None``/``0`` ⇒ equal weight.

    Returns:
        The multiplier rounded to 2 dp (e.g. ``1.4`` ⇒ 40% underestimate), or
        ``None`` if there aren't enough usable pairs to trust it.
    """
    now = now or utcnow()
    pairs: list[tuple[float, float, float]] = []
    for e in episodes:
        if e.get("predicted_value") and e.get("actual_value") is not None:
            w = decay_weight(e.get("timestamp"), now, half_life_days)
            pairs.append((e["predicted_value"], e["actual_value"], w))
    if len(pairs) < MIN_BIAS_SAMPLES:
        return None
    total_pred = sum(p * w for p, _, w in pairs)
    total_act = sum(a * w for _, a, w in pairs)
    if total_pred <= 0:
        return None
    return round(total_act / total_pred, 2)


def compute_bias_by_band(
    episodes: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
    timezone: str = "UTC",
) -> dict[str, float]:
    """Recompute the time-estimation bias **per time-of-day band** (learning §5).

    Buckets each predicted/actual episode by the local hour it occurred in, then
    runs the same recency-weighted :func:`compute_bias` within each band. A band
    with too few pairs (``< MIN_BIAS_SAMPLES``) is simply omitted, so we only
    condition where there's enough signal to trust — everything else keeps
    falling back to the global multiplier.

    Returns:
        ``{band: multiplier}`` for the bands that cleared the sample floor.
    """
    now = now or utcnow()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if not (e.get("predicted_value") and e.get("actual_value") is not None):
            continue
        hour = _local_hour(e.get("timestamp"), timezone)
        if hour is not None:
            groups[time_of_day_band(hour)].append(e)
    out: dict[str, float] = {}
    for band, eps in groups.items():
        band_bias = compute_bias(eps, now=now, half_life_days=half_life_days)
        if band_bias is not None:
            out[band] = band_bias
    return out


def resolve_bias(store: MemoryStore, *, local_hour: int | None = None) -> float:
    """The time-estimation multiplier to apply, preferring the time-of-day band.

    When ``local_hour`` is given and a band-specific bias has been learned for it,
    that wins; otherwise this falls back to the global ``time_estimation_bias``,
    then to ``1.0``. Consumers that know *when* a prediction applies (e.g. the
    "what fits right now" picker) pass the local hour to get context-conditioned
    calibration; those that don't keep the global behavior unchanged.
    """
    if local_hour is not None:
        banded = store.get_state(f"time_estimation_bias:{time_of_day_band(local_hour)}")
        if banded:
            try:
                return float(banded)
            except ValueError:
                pass
    return store.get_float("time_estimation_bias", 1.0)


def bias_calibration(
    episodes: list[dict[str, Any]],
    *,
    min_samples: int = MIN_CALIBRATION_SAMPLES,
    test_fraction: float = DEFAULT_CALIBRATION_TEST_FRACTION,
) -> BiasCalibration:
    """Measure whether the learned bias improves *subsequent* estimates (§4).

    Walk-forward, so it's honest rather than circular: sort the predicted/actual
    pairs by time, learn a bias (Σactual / Σpredicted) from the older *train*
    slice, then compare mean absolute error of raw vs bias-adjusted predictions on
    the newer *test* slice the bias never saw. ``improvement`` = raw − adjusted MAE
    (positive ⇒ the bias helped); ``helps`` is that verdict.

    Returns a ``status="insufficient"`` result (rather than raising) when there
    aren't enough pairs to split, so callers can surface "not enough data yet".

    Args:
        episodes: Episode dicts (only those with both predicted and actual count).
        min_samples: Minimum usable pairs before attempting the split.
        test_fraction: Fraction held out as the recent test slice.

    Returns:
        A :class:`BiasCalibration`.
    """
    pairs = [
        (_parse_ts(e.get("timestamp")) or datetime.min, e["predicted_value"], e["actual_value"])
        for e in episodes
        if e.get("predicted_value") and e.get("actual_value") is not None
    ]
    n = len(pairs)
    if n < min_samples:
        return BiasCalibration(status="insufficient", samples=n)
    pairs.sort(key=lambda t: t[0])
    test_size = max(2, round(n * test_fraction))
    train, test = pairs[: n - test_size], pairs[n - test_size :]
    if len(train) < MIN_BIAS_SAMPLES:
        return BiasCalibration(status="insufficient", samples=n)

    total_pred = sum(p for _, p, _ in train)
    if total_pred <= 0:
        return BiasCalibration(status="insufficient", samples=n)
    bias = sum(a for _, _, a in train) / total_pred

    raw_error = fmean(abs(p - a) for _, p, a in test)
    adjusted_error = fmean(abs(p * bias - a) for _, p, a in test)
    improvement = raw_error - adjusted_error
    return BiasCalibration(
        status="ok",
        samples=len(test),
        train_bias=round(bias, 2),
        raw_error=round(raw_error, 2),
        adjusted_error=round(adjusted_error, 2),
        improvement=round(improvement, 2),
        helps=improvement > 0,
    )


def recompute_patterns(
    store: MemoryStore,
    *,
    confidence_k: float = DEFAULT_CONFIDENCE_K,
    update_bias: bool = True,
    timezone: str = "UTC",
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
    now = utcnow()
    half_life_days = store.get_float("learning_half_life_days", DEFAULT_HALF_LIFE_DAYS)
    results = compute_patterns(
        episodes, confidence_k=confidence_k, now=now, half_life_days=half_life_days
    )
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
    calibration: BiasCalibration | None = None
    band_bias: dict[str, float] = {}
    if update_bias:
        bias = compute_bias(episodes, now=now, half_life_days=half_life_days)
        if bias is not None:
            store.set_state("time_estimation_bias", str(bias), source="inferred")
        # Context-conditioned (§5): a per-time-of-day multiplier where there's
        # enough signal, so a morning estimate is calibrated by morning history.
        band_bias = compute_bias_by_band(
            episodes, now=now, half_life_days=half_life_days, timezone=timezone
        )
        for band, value in band_bias.items():
            store.set_state(f"time_estimation_bias:{band}", str(value), source="inferred")
        # Close the loop: does that bias actually improve subsequent estimates?
        # Persist the verdict so the profile and CLI can surface a bad adaptation.
        calibration = bias_calibration(episodes)
        if calibration.status == "ok":
            helps_str = "true" if calibration.helps else "false"
            store.set_state("bias_calibration_helps", helps_str, source="inferred")
            store.set_state(
                "bias_calibration_improvement", str(calibration.improvement), source="inferred"
            )
            store.set_state("bias_calibration_samples", str(calibration.samples), source="inferred")

    return PatternRunSummary(
        episodes=len(episodes),
        patterns=len(results),
        by_type=dict(Counter(r.pattern_type for r in results)),
        bias=bias,
        calibration=calibration,
        band_bias=band_bias,
    )


# -- per-type helpers --------------------------------------------------------


def _time_estimation_patterns(
    episodes: list[dict[str, Any]], k: float, now: datetime, half_life_days: float | None
) -> list[PatternResult]:
    """Recency-weighted mean predicted/actual and variance per episode type."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if e.get("predicted_value") is not None and e.get("actual_value") is not None:
            groups[e["episode_type"]].append(e)

    results: list[PatternResult] = []
    for context_key, eps in groups.items():
        we = [(e, decay_weight(e.get("timestamp"), now, half_life_days)) for e in eps]
        observed = _wmean([(e["actual_value"], w) for e, w in we])
        predicted = _wmean([(e["predicted_value"], w) for e, w in we])
        if observed is None or predicted is None:
            continue
        results.append(
            PatternResult(
                pattern_type="time_estimation",
                context_key=context_key,
                observed_value=round(observed, 2),
                predicted_value=round(predicted, 2),
                variance=round(observed - predicted, 2),
                sample_size=len(eps),
                confidence=round(compute_confidence(sum(w for _, w in we), k), 3),
            )
        )
    return results


def _channel_response_patterns(
    episodes: list[dict[str, Any]], k: float, now: datetime, half_life_days: float | None
) -> list[PatternResult]:
    """Recency-weighted acknowledgement rate per delivery channel."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if e.get("channel") and e.get("acknowledged") is not None:
            groups[e["channel"]].append(e)

    results: list[PatternResult] = []
    for context_key, eps in groups.items():
        we = [(e, decay_weight(e.get("timestamp"), now, half_life_days)) for e in eps]
        ack_rate = _wmean([(1.0 if e["acknowledged"] else 0.0, w) for e, w in we])
        if ack_rate is None:
            continue
        results.append(
            PatternResult(
                pattern_type="channel_response",
                context_key=context_key,
                observed_value=round(ack_rate, 3),
                sample_size=len(eps),
                confidence=round(compute_confidence(sum(w for _, w in we), k), 3),
            )
        )
    return results


def _drift_patterns(
    episodes: list[dict[str, Any]], k: float, now: datetime, half_life_days: float | None
) -> list[PatternResult]:
    """Recency-weighted off-track score per episode type from outcomes."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if e.get("outcome") in DRIFT_WEIGHTS:
            groups[e["episode_type"]].append(e)

    results: list[PatternResult] = []
    for context_key, eps in groups.items():
        we = [(e, decay_weight(e.get("timestamp"), now, half_life_days)) for e in eps]
        score = _wmean([(DRIFT_WEIGHTS[e["outcome"]], w) for e, w in we])
        if score is None:
            continue
        results.append(
            PatternResult(
                pattern_type="drift",
                context_key=context_key,
                observed_value=round(score, 3),
                sample_size=len(eps),
                confidence=round(compute_confidence(sum(w for _, w in we), k), 3),
            )
        )
    return results
