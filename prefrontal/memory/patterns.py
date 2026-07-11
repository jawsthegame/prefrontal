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
- **context_switch** — mean switch-impulses per focus session and how many were
  deferred, from the per-session ``switch`` episodes the Impulsivity module logs
  on focus close (feeds `switch_rate_feedback` and the profile).

The computation is split into a **pure** :func:`compute_patterns` (list of
episode dicts in, list of :class:`PatternResult` out — trivially testable) and
:func:`recompute_patterns`, which wires it to a store and persists the results.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean
from typing import Any

from prefrontal.clock import local_datetime, local_hour_of
from prefrontal.clock import parse_ts as _parse_ts
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

#: When the walk-forward calibration check (§4) says the learned bias is *not*
#: improving recent estimates, don't just report it — shrink the global multiplier
#: toward the neutral 1.0 by this fraction of its deviation on the same learn pass.
#: 0.5 halves the deviation (1.4 → 1.2); 1.0 resets fully to 1.0; 0.0 disables the
#: auto-act and keeps the old report-only behavior. Tunable per user via the
#: ``bias_decay_on_miss`` coaching key.
DEFAULT_BIAS_DECAY_ON_MISS = 0.5

#: How much each outcome contributes to a drift score (higher = more off-track).
DRIFT_WEIGHTS = {"success": 0.0, "partial": 0.5, "miss": 1.0}

#: Learning the Time Blindness ``morning_prep`` cutoff (``early_start_threshold``)
#: from late morning departures. ``MIN`` late samples are needed before we tune it
#: off the seeded default; ``BAND`` is the local-hour window counted as "morning";
#: ``MARGIN`` minutes are added above the typical late-departure time so the cutoff
#: sits just past the struggle zone; and ``CLAMP`` keeps a learned cutoff sane.
MIN_EARLY_START_SAMPLES = 4
EARLY_START_MORNING_BAND = (4, 11)  # local hours [4, 11)
EARLY_START_MARGIN_MINUTES = 30
EARLY_START_CLAMP = (6 * 60, 10 * 60)  # 06:00 .. 10:00, as minutes-of-day

#: Recency half-life (days) for the learning pass: an episode this old counts
#: half as much as a fresh one, and the weight halves again every half-life after
#: that (exponential decay). This is what lets the profile *follow* a person who
#: has changed rather than being anchored to their cumulative history. Tunable per
#: user via the ``learning_half_life_days`` coaching key; set it to ``0`` to
#: disable decay and weigh all history equally (the pre-decay behavior).
DEFAULT_HALF_LIFE_DAYS = 30.0

#: The coaching key holding the global recency half-life (see above). A
#: **per-context** override lives at ``{HALF_LIFE_KEY}:{suffix}`` where ``suffix``
#: mirrors the context-conditioned bias key tail (``morning`` for a time-of-day
#: band, ``type:focus``, ``energy:high``, ``category:admin``) — so a context that
#: churns faster than the rest can decay faster, and one that's stable can decay
#: slower, without moving the global. Unset ⇒ that context uses the global.
HALF_LIFE_KEY = "learning_half_life_days"

#: Sliding-window length (days) for the learning pass: episodes older than this
#: are dropped *entirely* before any pattern/bias is computed — a hard cutoff on
#: top of the (softer) exponential decay, so ancient history can't drag on
#: estimates at all once a person has clearly moved on. Tunable via the
#: ``learning_window_days`` coaching key; ``0`` (the default) means **no window**
#: — the full history is considered, weighted only by decay (prior behavior).
DEFAULT_WINDOW_DAYS = 0.0

#: The coaching key holding the sliding-window length (see above).
WINDOW_KEY = "learning_window_days"

#: Coaching key toggling **auto-derivation** of per-context recency half-lives
#: from each context's own volatility. Off by default: the manual override +
#: global fallback are the baseline, and this is the adaptive layer on top. When
#: ``on``, the learn pass sets ``learning_half_life_days:<band>`` for time-of-day
#: bands that show enough signal — never touching a band the user set explicitly.
AUTO_HALF_LIFE_KEY = "auto_context_half_life"
#: A derived half-life is the global × a factor in ``[MIN, MAX]``: a *stable*
#: context (little drift between its older and recent bias) decays slower (trusts
#: more history, up to 2× the global); a *churny* one decays faster (down to 0.5×).
AUTO_HALF_LIFE_MIN_FACTOR = 0.5
AUTO_HALF_LIFE_MAX_FACTOR = 2.0
#: Relative drift (``|recent − older| / older`` bias) at/above which the factor
#: bottoms out at the min (follow as fast as we allow); ``0`` drift → the max.
AUTO_HALF_LIFE_FULL_DRIFT = 0.3
#: Absolute floor (days) under a derived half-life, so the factor math can't drive
#: a context down to a jittery, near-zero window.
AUTO_HALF_LIFE_FLOOR_DAYS = 7.0


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


def _local_hour(ts: Any, timezone: str) -> int | None:
    """Local wall-clock hour of a stored (naive-UTC) episode timestamp, or ``None``.

    Episodes are stored in UTC; the band a task *felt* like it happened in is the
    user's local hour, so we localize before bucketing. An unknown zone degrades
    to UTC rather than dropping the episode.
    """
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return local_hour_of(dt, timezone or "UTC")


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


def within_window(ts: Any, now: datetime, window_days: float | None) -> bool:
    """Is an episode timestamp inside the sliding learning window?

    ``True`` when ``window_days`` is falsy/≤0 (no window — keep everything), or the
    episode is at most ``window_days`` old. A missing/unparseable timestamp is
    **kept** (returns ``True``), mirroring :func:`decay_weight`'s forgiving stance:
    a bad clock shouldn't silently erase an episode. Future-dated stamps (clock
    skew) are in-window too.
    """
    if not window_days or window_days <= 0:
        return True
    t = _parse_ts(ts)
    if t is None:
        return True
    age_days = (now - t).total_seconds() / 86400.0
    return age_days <= window_days


def filter_to_window(
    episodes: list[dict[str, Any]], now: datetime, window_days: float | None
) -> list[dict[str, Any]]:
    """Drop episodes older than the sliding window (a no-op when it's disabled).

    Applied once, up front, by :func:`recompute_patterns`, so every downstream
    computation — patterns, the global bias, the per-context biases, and the
    walk-forward calibration — sees the *same* windowed slice and the raw sample
    counts (e.g. ``MIN_BIAS_SAMPLES``) reflect what's actually in the window.
    """
    if not window_days or window_days <= 0:
        return episodes
    return [e for e in episodes if within_window(e.get("timestamp"), now, window_days)]


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
class ChannelCalibration:
    """Does conditioning on channel actually predict acknowledgement? (§4)

    The channel-choice adaptation (``choose_channel`` bumping a cue up a rung when
    a channel is reliably ignored) is only worth trusting if a channel's *past*
    ack-rate predicts its *future* one. This walk-forward checks exactly that:
    learn each channel's ack-rate from the older *train* slice, then on the newer
    *test* slice compare the mean absolute error of predicting each ack with its
    channel's train rate (``adjusted``) versus with a single pooled train rate
    (``baseline``, i.e. no per-channel conditioning). ``improvement`` = baseline −
    adjusted (positive ⇒ conditioning on channel helped); ``helps`` is the verdict.

    Report-first, like the bias check began: it makes a noisy channel signal
    *visible* rather than silently driving escalation.
    """

    status: str  # "ok" | "insufficient"
    samples: int  # test-slice deliveries the error was measured on
    baseline_error: float | None = None
    adjusted_error: float | None = None
    improvement: float | None = None
    helps: bool = False


def decay_bias_toward_neutral(bias: float, factor: float = DEFAULT_BIAS_DECAY_ON_MISS) -> float:
    """Shrink a time-estimation multiplier toward the neutral 1.0 (§4 auto-act).

    ``1.0 + (bias - 1.0) * (1 - factor)`` — ``factor`` is the *fraction of the
    deviation from 1.0 to remove*: ``0.0`` leaves the multiplier unchanged (auto-
    act disabled), ``0.5`` halves the deviation (1.4 → 1.2), ``1.0`` resets fully
    to neutral. ``factor`` is clamped to ``[0, 1]`` so a stray config value can
    only ever move the multiplier *toward* 1.0, never past it or away from it.

    This is the corrective applied when the walk-forward check finds the learned
    bias is hurting recent estimates: rather than waiting for recency decay to
    erode a stale adaptation, actively pull it back so it stops overshooting
    before the next learn pass.
    """
    factor = min(1.0, max(0.0, factor))
    return round(1.0 + (bias - 1.0) * (1.0 - factor), 2)


@dataclass(frozen=True)
class PatternRunSummary:
    """Summary of a single :func:`recompute_patterns` run."""

    episodes: int
    patterns: int
    by_type: dict[str, int] = field(default_factory=dict)
    bias: float | None = None
    #: The global multiplier before the §4 auto-act ran, set only when a
    #: "not helping" verdict decayed it toward 1.0 (so ``bias`` is the post value
    #: and this is the pre value). ``None`` when nothing was auto-decayed.
    bias_pre_decay: float | None = None
    calibration: BiasCalibration | None = None
    #: Walk-forward verdict on the channel-choice adaptation (§4), report-first.
    channel_calibration: ChannelCalibration | None = None
    band_bias: dict[str, float] = field(default_factory=dict)
    type_bias: dict[str, float] = field(default_factory=dict)
    energy_bias: dict[str, float] = field(default_factory=dict)
    category_bias: dict[str, float] = field(default_factory=dict)
    #: Per-activity multipliers (``outing``/``focus``/…), finer than ``type_bias``.
    activity_bias: dict[str, float] = field(default_factory=dict)
    #: Sliding-window length in effect this pass (``0`` = no window). ``windowed_out``
    #: is how many episodes the window excluded before learning (0 when disabled).
    window_days: float = 0.0
    windowed_out: int = 0
    #: Per-context recency half-lives auto-derived this pass, keyed by the context
    #: suffix (``morning``, ``type:focus``, ``energy:high``, ``category:admin``),
    #: when ``auto_context_half_life`` is on; empty otherwise.
    auto_half_lives: dict[str, float] = field(default_factory=dict)
    #: The Time Blindness ``morning_prep`` cutoff learned from late morning
    #: departures this pass (``HH:MM``), or ``None`` when there wasn't enough signal
    #: to move off the current/seeded default.
    early_start_threshold: str | None = None


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
    results += _context_switch_patterns(episodes, confidence_k, now, half_life_days)
    return results


def _is_estimation_pair(e: dict[str, Any]) -> bool:
    """Whether an episode's predicted/actual are duration estimates (minutes).

    The ``switch`` type overloads ``predicted_value``/``actual_value`` with
    switch-impulse *counts* (see :func:`hyperfocus._log_switch_episode`), consumed
    only by :func:`_context_switch_patterns`. Excluding it keeps that count data
    out of every time-estimation / bias / calibration computation, which would
    otherwise read the counts as minutes and skew the global multiplier.
    """
    return (
        e.get("episode_type") != "switch"
        and bool(e.get("predicted_value"))
        and e.get("actual_value") is not None
    )


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
        if _is_estimation_pair(e):
            w = decay_weight(e.get("timestamp"), now, half_life_days)
            pairs.append((e["predicted_value"], e["actual_value"], w))
    if len(pairs) < MIN_BIAS_SAMPLES:
        return None
    total_pred = sum(p * w for p, _, w in pairs)
    total_act = sum(a * w for _, a, w in pairs)
    if total_pred <= 0:
        return None
    return round(total_act / total_pred, 2)


def _resolve_half_life(
    half_life_for: Callable[[str], float | None] | None,
    suffix: str,
    default: float | None,
) -> float | None:
    """Per-context half-life for ``suffix``, falling back to the global ``default``.

    ``half_life_for`` maps a context suffix (``morning``, ``type:focus``, …) to its
    override half-life, or ``None`` when that context has no override. A returned
    ``0`` is honored (an explicit "no decay for this context"), distinct from
    ``None`` (fall back to the global).
    """
    if half_life_for is not None:
        override = half_life_for(suffix)
        if override is not None:
            return override
    return default


def compute_bias_by_band(
    episodes: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
    timezone: str = "UTC",
    half_life_for: Callable[[str], float | None] | None = None,
) -> dict[str, float]:
    """Recompute the time-estimation bias **per time-of-day band** (learning §5).

    Buckets each predicted/actual episode by the local hour it occurred in, then
    runs the same recency-weighted :func:`compute_bias` within each band. A band
    with too few pairs (``< MIN_BIAS_SAMPLES``) is simply omitted, so we only
    condition where there's enough signal to trust — everything else keeps
    falling back to the global multiplier.

    ``half_life_for`` supplies an optional **per-context half-life** (looked up by
    the band name, e.g. ``morning``); a band with no override decays at the global
    ``half_life_days``.

    Returns:
        ``{band: multiplier}`` for the bands that cleared the sample floor.
    """
    now = now or utcnow()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if not _is_estimation_pair(e):
            continue
        hour = _local_hour(e.get("timestamp"), timezone)
        if hour is not None:
            groups[time_of_day_band(hour)].append(e)
    out: dict[str, float] = {}
    for band, eps in groups.items():
        hl = _resolve_half_life(half_life_for, band, half_life_days)
        band_bias = compute_bias(eps, now=now, half_life_days=hl)
        if band_bias is not None:
            out[band] = band_bias
    return out


#: State-key prefixes for the context-conditioned biases (learning §5). Each is
#: namespaced so the dimensions never collide with each other or the time-of-day
#: band keys (``time_estimation_bias:morning`` etc.).
TYPE_BIAS_PREFIX = "time_estimation_bias:type:"
ENERGY_BIAS_PREFIX = "time_estimation_bias:energy:"
CATEGORY_BIAS_PREFIX = "time_estimation_bias:category:"
#: ...and one step **finer than episode type**: the *activity* a task episode came
#: from, read off its ``context`` prefix (``outing`` / ``focus`` / ``trip`` / …).
#: Every user-touch surface logs a ``task`` episode, so ``type:task`` pools a coffee
#: run ("back in 15" that stretches to 45) with a well-estimated focus block; this
#: dimension separates them so an outing calibrates against *outing* history, not
#: the shared task multiplier (Module-1 / focus-balance "finer context_key" items).
ACTIVITY_BIAS_PREFIX = "time_estimation_bias:activity:"


def episode_activity(e: dict[str, Any]) -> str | None:
    """The coarse *activity* a task episode came from, or ``None`` if unlabeled.

    Read off the ``context`` field the loggers already stamp — the leading word
    before any ``:`` or space, lowercased: ``"outing: coffee"`` → ``"outing"``,
    ``"outing abandoned: …"`` → ``"outing"``, ``"focus: deep work"`` → ``"focus"``,
    ``"trip"`` → ``"trip"``. This is finer than ``episode_type`` (all of these are
    ``task``) but coarser than the specific intention, so histories of the *same
    kind* of out-of-flow time bucket together. A blank/None context yields ``None``
    (the episode simply doesn't condition this dimension). It's derived generically
    rather than from an allowlist, so a new activity gets its own bucket for free —
    the ``MIN_BIAS_SAMPLES`` floor drops any that never gather real signal.
    """
    context = e.get("context")
    if not context:
        return None
    head = str(context).split(":", 1)[0].strip()
    first = head.split()[0].lower() if head.split() else ""
    return first or None


def _bias_by_group(
    episodes: list[dict[str, Any]],
    key_fn: Any,
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
    context_prefix: str = "",
    half_life_for: Callable[[str], float | None] | None = None,
) -> dict[str, float]:
    """Recency-weighted :func:`compute_bias` within each ``key_fn(e)`` bucket.

    Shared machinery for every context-conditioned bias dimension: keep only the
    predicted/actual episodes, bucket them by ``key_fn`` (a falsy key drops the
    episode from conditioning), and compute a bias per bucket, omitting any that
    fall below ``MIN_BIAS_SAMPLES`` so we only condition where there's real signal.

    ``context_prefix`` + the bucket key forms the suffix a per-context half-life is
    looked up under (``type:``+``focus`` → ``type:focus``), so ``half_life_for`` can
    give each bucket its own decay; a bucket with no override uses ``half_life_days``.
    """
    now = now or utcnow()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if not _is_estimation_pair(e):
            continue
        key = key_fn(e)
        if key:
            groups[key].append(e)
    out: dict[str, float] = {}
    for key, eps in groups.items():
        hl = _resolve_half_life(half_life_for, f"{context_prefix}{key}", half_life_days)
        group_bias = compute_bias(eps, now=now, half_life_days=hl)
        if group_bias is not None:
            out[key] = group_bias
    return out


def compute_bias_by_type(
    episodes: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
    half_life_for: Callable[[str], float | None] | None = None,
) -> dict[str, float]:
    """Recompute the time-estimation bias **per episode type** (learning §5).

    The global multiplier pools every kind of prediction together, but a person
    mis-estimates a focus block differently from a departure buffer or an outing.
    This buckets the predicted/actual episodes by ``episode_type`` and runs the
    same recency-weighted :func:`compute_bias` within each. A type with too few
    pairs (``< MIN_BIAS_SAMPLES``) is omitted, so we only condition where there's
    enough signal — everything else falls back to the global multiplier.

    (Todo closes log a ``task`` episode with ``actual_value=None`` on purpose —
    created→closed is wall-clock, not time on task — so they don't feed this; the
    ``task`` bias is learned from focus sessions, the real task-duration signal.)

    Returns:
        ``{episode_type: multiplier}`` for the types that cleared the floor.
    """
    return _bias_by_group(
        episodes,
        lambda e: e.get("episode_type"),
        now=now,
        half_life_days=half_life_days,
        context_prefix="type:",
        half_life_for=half_life_for,
    )


def _normalized_tag(value: Any) -> str | None:
    """Lowercase/trim a tag (energy/category), or ``None`` when blank."""
    return (str(value).strip().lower() or None) if value is not None else None


def compute_bias_by_energy(
    episodes: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
    half_life_for: Callable[[str], float | None] | None = None,
) -> dict[str, float]:
    """Recompute the bias **per task energy load** (low/medium/high) (learning §5).

    Conditions on the ``energy`` tag stamped on task episodes from the todo a
    focus block was working — a "quick" high-energy task tends to blow its
    estimate more than a low-energy one. Only tagged predicted/actual episodes
    contribute; untagged history falls back to the coarser dimensions.

    Returns:
        ``{energy: multiplier}`` for the loads that cleared the sample floor.
    """
    return _bias_by_group(
        episodes,
        lambda e: _normalized_tag(e.get("energy")),
        now=now,
        half_life_days=half_life_days,
        context_prefix="energy:",
        half_life_for=half_life_for,
    )


def compute_bias_by_category(
    episodes: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
    half_life_for: Callable[[str], float | None] | None = None,
) -> dict[str, float]:
    """Recompute the bias **per task category** (learning §5).

    Conditions on the ``category`` tag stamped on task episodes from the linked
    todo, so e.g. "admin" and "creative" work calibrate separately. Same signal
    floor and fallback behavior as the other dimensions.

    Returns:
        ``{category: multiplier}`` for the categories that cleared the floor.
    """
    return _bias_by_group(
        episodes,
        lambda e: _normalized_tag(e.get("category")),
        now=now,
        half_life_days=half_life_days,
        context_prefix="category:",
        half_life_for=half_life_for,
    )


def compute_bias_by_activity(
    episodes: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
    half_life_for: Callable[[str], float | None] | None = None,
) -> dict[str, float]:
    """Recompute the bias **per activity** — finer than episode type (learning §5).

    Every out-of-flow surface (outings, focus blocks, trips) logs an
    ``episode_type="task"`` pair, so the ``type:task`` multiplier pools them all.
    This buckets those pairs by :func:`episode_activity` (from the episode's
    ``context``) so a coffee-run outing calibrates against *outing* history rather
    than being averaged in with focus sessions. Same signal floor and fallback
    behavior as the other dimensions — an activity below ``MIN_BIAS_SAMPLES`` is
    omitted and its consumers keep falling back to the coarser layers.

    Returns:
        ``{activity: multiplier}`` for the activities that cleared the floor.
    """
    return _bias_by_group(
        episodes,
        episode_activity,
        now=now,
        half_life_days=half_life_days,
        context_prefix="activity:",
        half_life_for=half_life_for,
    )


def resolve_bias(
    store: MemoryStore,
    *,
    local_hour: int | None = None,
    episode_type: str | None = None,
    energy: str | None = None,
    category: str | None = None,
    activity: str | None = None,
) -> float:
    """The time-estimation multiplier to apply, preferring the most specific signal.

    Precedence (each layer consulted only when the caller supplies its context
    *and* enough signal was learned there, else it falls through):

    1. **time-of-day band** for ``local_hour`` — the finer, §4-calibration-validated
       signal for the "what fits" path, kept first so its shipped behavior is
       preserved;
    2. **energy** load — the most task-intrinsic signal (how heavy the task is);
    3. **category** of the task;
    4. **activity** — the kind of out-of-flow time (``outing`` / ``focus`` / …),
       finer than the episode type it refines (so an outing prefers *outing*
       history over the pooled ``task`` multiplier);
    5. **episode type** — the coarse kind of prediction;
    6. the global ``time_estimation_bias``; then ``1.0``.

    A consumer that supplies none keeps the global behavior unchanged; one that
    supplies several gets each finer layer as a fallback when the layer above it
    has no data yet — so adding ``energy``/``category``/``activity`` never regresses
    a caller whose band already resolves.
    """

    def _state_float(key: str) -> float | None:
        raw = store.get_state(key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                return None
        return None

    candidates: list[str] = []
    if local_hour is not None:
        candidates.append(f"time_estimation_bias:{time_of_day_band(local_hour)}")
    if energy:
        candidates.append(f"{ENERGY_BIAS_PREFIX}{_normalized_tag(energy)}")
    if category:
        candidates.append(f"{CATEGORY_BIAS_PREFIX}{_normalized_tag(category)}")
    if activity:
        candidates.append(f"{ACTIVITY_BIAS_PREFIX}{_normalized_tag(activity)}")
    if episode_type:
        candidates.append(f"{TYPE_BIAS_PREFIX}{episode_type}")
    for key in candidates:
        value = _state_float(key)
        if value is not None:
            return value
    return store.get_float("time_estimation_bias", 1.0)


def task_bias_resolver(store: MemoryStore, *, local_hour: int | None = None):
    """A ``todo -> multiplier`` resolver for the "what fits" path (learning §5).

    Bundles the full precedence for a *task* estimate — time-of-day band, then the
    todo's energy, then its category, then the ``task`` episode-type bias, then
    global — so :func:`~prefrontal.scheduling.fit_todos` can pad each todo by its
    own learned bias via ``bias_fn``. Pass ``local_hour`` when the fit happens at a
    known moment (all the point-in-time pickers do).
    """
    return lambda todo: resolve_bias(
        store,
        local_hour=local_hour,
        episode_type="task",
        energy=todo.get("energy"),
        category=todo.get("category"),
    )


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
        if _is_estimation_pair(e)
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


def channel_calibration(
    episodes: list[dict[str, Any]],
    *,
    min_samples: int = MIN_CALIBRATION_SAMPLES,
    test_fraction: float = DEFAULT_CALIBRATION_TEST_FRACTION,
) -> ChannelCalibration:
    """Measure whether per-channel ack-rates predict future acks better than pooled (§4).

    The walk-forward twin of :func:`bias_calibration` for the channel-choice
    adaptation. Sorts the delivered nudges (those with a ``channel`` and a known
    ``acknowledged``) by time, learns each channel's ack-rate plus one pooled rate
    from the older *train* slice, then on the newer *test* slice compares the mean
    absolute error of predicting each ack with its channel's rate (``adjusted``)
    versus the pooled rate (``baseline``). ``improvement`` = baseline − adjusted;
    ``helps`` when conditioning on channel predicted held-out acks better.

    Returns ``status="insufficient"`` (never raises) when there aren't enough
    deliveries to split.
    """
    pairs: list[tuple[datetime, str, float]] = []
    for e in episodes:
        if e.get("channel") and e.get("acknowledged") is not None:
            when = _parse_ts(e.get("timestamp")) or datetime.min
            pairs.append((when, e["channel"], float(bool(e["acknowledged"]))))
    n = len(pairs)
    if n < min_samples:
        return ChannelCalibration(status="insufficient", samples=n)
    pairs.sort(key=lambda t: t[0])
    test_size = max(2, round(n * test_fraction))
    train, test = pairs[: n - test_size], pairs[n - test_size :]
    if len(train) < MIN_BIAS_SAMPLES:
        return ChannelCalibration(status="insufficient", samples=n)

    pooled = fmean(a for _, _, a in train)
    by_channel: dict[str, list[float]] = defaultdict(list)
    for _, ch, a in train:
        by_channel[ch].append(a)
    channel_rate = {ch: fmean(acks) for ch, acks in by_channel.items()}

    baseline_error = fmean(abs(pooled - a) for _, _, a in test)
    adjusted_error = fmean(abs(channel_rate.get(ch, pooled) - a) for _, ch, a in test)
    improvement = baseline_error - adjusted_error
    return ChannelCalibration(
        status="ok",
        samples=len(test),
        baseline_error=round(baseline_error, 3),
        adjusted_error=round(adjusted_error, 3),
        improvement=round(improvement, 3),
        helps=improvement > 0,
    )


def compute_early_start_threshold(
    episodes: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
    timezone: str = "UTC",
) -> str | None:
    """Learn the Time Blindness ``morning_prep`` cutoff from late morning departures.

    The ``morning_prep`` nudge fires the evening before a commitment whose leave-by
    beats ``early_start_threshold``. Rather than a fixed default, tune that cutoff to
    *cover the mornings the user actually struggles with*: take the ``departure``
    episodes that came in **late** (``outcome == "miss"``) in the morning band,
    recency-weight them, and set the cutoff to their mean clock time plus a small
    margin — so the times you tend to run late all fall under it (and earn the alarm
    heads-up), while reliably-fine later mornings don't.

    Only late departures inform it, so the cutoff moves *later* the later you run
    late (nudging more mornings) and stays put when you're on time. Returns ``None``
    (keep the current/seeded default) until there are at least
    :data:`MIN_EARLY_START_SAMPLES` late morning departures — we never invent a
    cutoff for someone with no morning trouble. Uses the actual departure time as a
    coarse proxy for how early the morning was (the exact leave-by isn't on the
    episode); the margin absorbs its slight lateness bias. Clamped to
    :data:`EARLY_START_CLAMP`.

    Returns:
        The learned cutoff as ``HH:MM``, or ``None`` when there isn't enough signal.
    """
    now = now or utcnow()
    lo, hi = EARLY_START_MORNING_BAND
    weighted: list[tuple[float, float]] = []
    for e in episodes:
        if e.get("episode_type") != "departure" or e.get("outcome") != "miss":
            continue
        dt = _parse_ts(e.get("timestamp"))
        if dt is None:
            continue
        local = local_datetime(dt, timezone or "UTC")
        if not (lo <= local.hour < hi):
            continue
        w = decay_weight(e.get("timestamp"), now, half_life_days)
        weighted.append((local.hour * 60 + local.minute, w))
    if len(weighted) < MIN_EARLY_START_SAMPLES:
        return None
    total_w = sum(w for _, w in weighted)
    if total_w <= 0:
        return None
    mean_min = sum(m * w for m, w in weighted) / total_w
    clamp_lo, clamp_hi = EARLY_START_CLAMP
    cutoff = max(clamp_lo, min(clamp_hi, mean_min + EARLY_START_MARGIN_MINUTES))
    h, m = divmod(int(round(cutoff)), 60)
    return f"{h:02d}:{m:02d}"


def _make_half_life_resolver(store: MemoryStore) -> Callable[[str], float | None]:
    """Build the per-context half-life lookup used by the bias-by-context passes.

    Returns a function mapping a context suffix (``morning``, ``type:focus``,
    ``energy:high``, ``category:admin``) to the half-life at
    ``learning_half_life_days:<suffix>``, or ``None`` when no override is set (so
    the caller falls back to the global). A ``0`` override is honored (no decay for
    that context); a blank/unparseable value is treated as unset.
    """

    def resolve(suffix: str) -> float | None:
        raw = store.get_state(f"{HALF_LIFE_KEY}:{suffix}")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    return resolve


def derive_half_life(
    episodes: list[dict[str, Any]],
    global_half_life: float | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Suggest a recency half-life for one context from its volatility, or ``None``.

    Splits the context's predicted/actual pairs in time order and compares the
    (equal-weight) bias of the older half to the recent half. A context whose bias
    has **drifted** — churn — should decay *faster* so learning follows the change;
    a **stable** one should decay *slower* to trust more of its history. The result
    is ``global_half_life × factor`` where the factor runs from
    :data:`AUTO_HALF_LIFE_MAX_FACTOR` at zero drift down to
    :data:`AUTO_HALF_LIFE_MIN_FACTOR` at :data:`AUTO_HALF_LIFE_FULL_DRIFT`, floored
    at :data:`AUTO_HALF_LIFE_FLOOR_DAYS`.

    Returns ``None`` when decay is disabled (``global_half_life`` falsy/≤0), there
    aren't enough pairs to split (``< 2 × MIN_BIAS_SAMPLES``, each half clearing
    ``MIN_BIAS_SAMPLES``), or the older-half bias is non-positive.
    """
    if not global_half_life or global_half_life <= 0:
        return None
    now = now or utcnow()
    pairs = [
        e for e in episodes
        if _is_estimation_pair(e)
    ]
    if len(pairs) < 2 * MIN_BIAS_SAMPLES:
        return None
    pairs.sort(key=lambda e: str(e.get("timestamp") or ""))
    mid = len(pairs) // 2
    # Equal-weight bias of each half (half_life_days=None) so the drift measure
    # reflects the raw levels, not values already pre-decayed.
    older = compute_bias(pairs[:mid], now=now, half_life_days=None)
    recent = compute_bias(pairs[mid:], now=now, half_life_days=None)
    if older is None or recent is None or older <= 0:
        return None
    rel_drift = abs(recent - older) / older
    frac = min(1.0, rel_drift / AUTO_HALF_LIFE_FULL_DRIFT) if AUTO_HALF_LIFE_FULL_DRIFT else 1.0
    span = AUTO_HALF_LIFE_MAX_FACTOR - AUTO_HALF_LIFE_MIN_FACTOR
    factor = AUTO_HALF_LIFE_MAX_FACTOR - span * frac
    return round(max(AUTO_HALF_LIFE_FLOOR_DAYS, global_half_life * factor), 1)


def derive_band_half_lives(
    episodes: list[dict[str, Any]],
    global_half_life: float | None,
    *,
    now: datetime | None = None,
    timezone: str = "UTC",
) -> dict[str, float]:
    """Auto-derived recency half-life per time-of-day band (``{band: days}``).

    Buckets the predicted/actual episodes by local band (as
    :func:`compute_bias_by_band` does) and runs :func:`derive_half_life` in each.
    Bands without enough signal are omitted. Empty when decay is off.
    """
    now = now or utcnow()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if not _is_estimation_pair(e):
            continue
        hour = _local_hour(e.get("timestamp"), timezone)
        if hour is not None:
            groups[time_of_day_band(hour)].append(e)
    out: dict[str, float] = {}
    for band, eps in groups.items():
        hl = derive_half_life(eps, global_half_life, now=now)
        if hl is not None:
            out[band] = hl
    return out


def _half_life_by_group(
    episodes: list[dict[str, Any]],
    key_fn: Any,
    global_half_life: float | None,
    *,
    now: datetime | None = None,
    context_prefix: str = "",
) -> dict[str, float]:
    """:func:`derive_half_life` within each ``key_fn(e)`` bucket (the half-life twin
    of :func:`_bias_by_group`). Keys are ``context_prefix`` + the bucket key, so the
    result maps the exact suffix a per-context half-life is stored under."""
    now = now or utcnow()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if not _is_estimation_pair(e):
            continue
        key = key_fn(e)
        if key:
            groups[key].append(e)
    out: dict[str, float] = {}
    for key, eps in groups.items():
        hl = derive_half_life(eps, global_half_life, now=now)
        if hl is not None:
            out[f"{context_prefix}{key}"] = hl
    return out


def derive_context_half_lives(
    episodes: list[dict[str, Any]],
    global_half_life: float | None,
    *,
    now: datetime | None = None,
    timezone: str = "UTC",
) -> dict[str, float]:
    """Auto-derived recency half-life for **every** learning context (``{suffix: days}``).

    Keys are the same suffixes the bias-by-context passes and
    :func:`_make_half_life_resolver` use: a bare time-of-day band (``morning``), or
    a prefixed tag (``type:focus``, ``energy:high``, ``category:admin``). Each is
    derived from that context's own drift via :func:`derive_half_life`; contexts
    without enough signal are omitted, and the map is empty when decay is off.
    """
    now = now or utcnow()
    out = dict(derive_band_half_lives(episodes, global_half_life, now=now, timezone=timezone))
    out.update(_half_life_by_group(
        episodes, lambda e: e.get("episode_type"), global_half_life,
        now=now, context_prefix="type:",
    ))
    out.update(_half_life_by_group(
        episodes, lambda e: _normalized_tag(e.get("energy")), global_half_life,
        now=now, context_prefix="energy:",
    ))
    out.update(_half_life_by_group(
        episodes, lambda e: _normalized_tag(e.get("category")), global_half_life,
        now=now, context_prefix="category:",
    ))
    out.update(_half_life_by_group(
        episodes, episode_activity, global_half_life,
        now=now, context_prefix="activity:",
    ))
    return out


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
    now = utcnow()
    half_life_days = store.get_float(HALF_LIFE_KEY, DEFAULT_HALF_LIFE_DAYS)
    window_days = store.get_float(WINDOW_KEY, DEFAULT_WINDOW_DAYS)
    # Sliding window (learning bucketing): drop stale episodes *entirely* before
    # anything is computed, so patterns, biases, and the walk-forward calibration
    # all see the same windowed slice and raw sample floors count only what's in it.
    all_episodes = store.all_episodes()
    episodes = filter_to_window(all_episodes, now, window_days)
    windowed_out = len(all_episodes) - len(episodes)
    # Per-context half-life (learning bucketing): a context can override the global
    # decay via ``learning_half_life_days:<suffix>`` (e.g. ``:type:focus``), so a
    # churny context can follow faster and a stable one slower. Unset ⇒ the global.
    half_life_for = _make_half_life_resolver(store)
    # Auto-derived per-context half-lives (opt-in): before the bias passes, set
    # each context's `learning_half_life_days:<suffix>` from its own volatility, so
    # a churny context (time-of-day band, episode type, energy, or category) decays
    # faster and a stable one slower without the user hand-tuning it. Never
    # overrides a context the user set explicitly; the resolver above reads state
    # lazily, so this pass's context biases use the fresh values.
    auto_half_lives: dict[str, float] = {}
    if (store.get_state(AUTO_HALF_LIFE_KEY, "off") or "off") == "on":
        state_all = store.all_state()
        for suffix, hl in derive_context_half_lives(
            episodes, half_life_days, now=now, timezone=timezone
        ).items():
            key = f"{HALF_LIFE_KEY}:{suffix}"
            if (state_all.get(key, {}) or {}).get("source") == "explicit":
                continue  # respect a hand-set override
            store.set_state(key, str(hl), source="inferred")
            auto_half_lives[suffix] = hl
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
    bias_pre_decay: float | None = None
    calibration: BiasCalibration | None = None
    band_bias: dict[str, float] = {}
    type_bias: dict[str, float] = {}
    energy_bias: dict[str, float] = {}
    category_bias: dict[str, float] = {}
    activity_bias: dict[str, float] = {}
    if update_bias:
        bias = compute_bias(episodes, now=now, half_life_days=half_life_days)
        if bias is not None:
            store.set_state("time_estimation_bias", str(bias), source="inferred")
        # Context-conditioned (§5): a per-time-of-day multiplier where there's
        # enough signal, so a morning estimate is calibrated by morning history.
        band_bias = compute_bias_by_band(
            episodes,
            now=now,
            half_life_days=half_life_days,
            timezone=timezone,
            half_life_for=half_life_for,
        )
        for band, value in band_bias.items():
            store.set_state(f"time_estimation_bias:{band}", str(value), source="inferred")
        # ...and per episode type (§5, task-type dimension): a focus block is
        # mis-estimated differently from a departure buffer, so calibrate by kind.
        type_bias = compute_bias_by_type(
            episodes, now=now, half_life_days=half_life_days, half_life_for=half_life_for
        )
        for etype, value in type_bias.items():
            store.set_state(f"{TYPE_BIAS_PREFIX}{etype}", str(value), source="inferred")
        # ...and per task energy load / category (§5), from the tags focus blocks
        # stamp onto their close episodes via the linked todo.
        energy_bias = compute_bias_by_energy(
            episodes, now=now, half_life_days=half_life_days, half_life_for=half_life_for
        )
        for level, value in energy_bias.items():
            store.set_state(f"{ENERGY_BIAS_PREFIX}{level}", str(value), source="inferred")
        category_bias = compute_bias_by_category(
            episodes, now=now, half_life_days=half_life_days, half_life_for=half_life_for
        )
        for cat, value in category_bias.items():
            store.set_state(f"{CATEGORY_BIAS_PREFIX}{cat}", str(value), source="inferred")
        # ...and one step finer than episode type: per *activity* (outing / focus /
        # trip, from the episode context), so a coffee run calibrates against outing
        # history rather than pooling into the shared task multiplier.
        activity_bias = compute_bias_by_activity(
            episodes, now=now, half_life_days=half_life_days, half_life_for=half_life_for
        )
        for act, value in activity_bias.items():
            store.set_state(f"{ACTIVITY_BIAS_PREFIX}{act}", str(value), source="inferred")
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
            # Auto-act (§4): a bias the walk-forward check found *not* helping is
            # actively pulled toward the neutral 1.0 rather than only reported, so
            # a stale adaptation stops overshooting estimates before the next pass.
            # Skip when it's already neutral (nothing to correct) or the user has
            # disabled the auto-act (``bias_decay_on_miss`` = 0 → report-only).
            decay = store.get_float("bias_decay_on_miss", DEFAULT_BIAS_DECAY_ON_MISS)
            decayed = "false"
            if not calibration.helps and bias is not None and bias != 1.0 and decay > 0:
                adjusted = decay_bias_toward_neutral(bias, decay)
                if adjusted != bias:
                    store.set_state(
                        "time_estimation_bias", str(adjusted), source="inferred"
                    )
                    bias_pre_decay = bias
                    bias = adjusted
                    decayed = "true"
            store.set_state("bias_calibration_decayed", decayed, source="inferred")

    # Close the loop on the *channel-choice* adaptation too (§4): does conditioning
    # on channel predict held-out acks better than a pooled rate? Report-first —
    # persisted for the profile/CLI, no auto-act yet (a bad channel signal is made
    # visible rather than silently driving escalation). Independent of the bias, so
    # it runs regardless of `update_bias`.
    channel_cal = channel_calibration(episodes)
    if channel_cal.status == "ok":
        store.set_state(
            "channel_calibration_helps", "true" if channel_cal.helps else "false",
            source="inferred",
        )
        store.set_state(
            "channel_calibration_improvement", str(channel_cal.improvement), source="inferred"
        )
        store.set_state(
            "channel_calibration_samples", str(channel_cal.samples), source="inferred"
        )

    # Learn the Time Blindness morning_prep cutoff (``early_start_threshold``) from
    # late morning departures, so the evening "set an alarm" heads-up covers the
    # mornings this user actually runs late for. Respects a hand-set override (like
    # the auto half-life pass) and only moves off the default once there's signal.
    early_start_threshold = compute_early_start_threshold(
        episodes, now=now, half_life_days=half_life_days, timezone=timezone
    )
    if early_start_threshold is not None:
        source = (store.all_state().get("early_start_threshold", {}) or {}).get("source")
        if source != "explicit":
            store.set_state("early_start_threshold", early_start_threshold, source="inferred")

    return PatternRunSummary(
        episodes=len(episodes),
        patterns=len(results),
        by_type=dict(Counter(r.pattern_type for r in results)),
        bias=bias,
        bias_pre_decay=bias_pre_decay,
        calibration=calibration,
        channel_calibration=channel_cal,
        band_bias=band_bias,
        type_bias=type_bias,
        energy_bias=energy_bias,
        category_bias=category_bias,
        activity_bias=activity_bias,
        window_days=window_days,
        windowed_out=windowed_out,
        auto_half_lives=auto_half_lives,
        early_start_threshold=early_start_threshold,
    )


# -- per-type helpers --------------------------------------------------------


def _time_estimation_patterns(
    episodes: list[dict[str, Any]], k: float, now: datetime, half_life_days: float | None
) -> list[PatternResult]:
    """Recency-weighted mean predicted/actual and variance per episode type."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        # Skip `switch` — it stores impulse counts in predicted/actual, not
        # durations (its own pattern is built by `_context_switch_patterns`).
        if (
            e.get("episode_type") != "switch"
            and e.get("predicted_value") is not None
            and e.get("actual_value") is not None
        ):
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


def _context_switch_patterns(
    episodes: list[dict[str, Any]], k: float, now: datetime, half_life_days: float | None
) -> list[PatternResult]:
    """Recency-weighted switch-impulse rate per focus context (Impulsivity §8).

    Derives from the per-session ``switch`` episodes logged on focus close
    (``predicted_value`` = impulses signalled, ``actual_value`` = deferred). Per
    ``context_key`` (``'focus'`` for all focus blocks): ``observed_value`` = mean
    impulses/session, ``predicted_value`` = mean deferred/session, ``variance`` =
    mean *honored* (impulses − deferred) — the switches acted on. This was the
    loop the roadmap flagged as blocked on captured switch events.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in episodes:
        if e.get("episode_type") == "switch" and e.get("predicted_value") is not None:
            groups[e.get("context") or "focus"].append(e)

    results: list[PatternResult] = []
    for context_key, eps in groups.items():
        we = [(e, decay_weight(e.get("timestamp"), now, half_life_days)) for e in eps]
        impulses = _wmean([(float(e["predicted_value"]), w) for e, w in we])
        deferred = _wmean([(float(e.get("actual_value") or 0.0), w) for e, w in we])
        if impulses is None:
            continue
        deferred = deferred or 0.0
        results.append(
            PatternResult(
                pattern_type="context_switch",
                context_key=context_key,
                observed_value=round(impulses, 3),
                predicted_value=round(deferred, 3),
                variance=round(max(impulses - deferred, 0.0), 3),
                sample_size=len(eps),
                confidence=round(compute_confidence(sum(w for _, w in we), k), 3),
            )
        )
    return results
