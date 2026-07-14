"""Learned per-user receptivity model — *when* is a coach nudge welcome? (M3)

The coaching engine's rules-based receptivity gate (:func:`prefrontal.coaching.receptive`)
answers "have they stopped answering?" with one blunt signal: a run of consecutive
*ignored* coach nudges. This module is the graduation the roadmap (M3 / divergence
#6) named — the *learned* form: a transparent, per-user **contextual
acknowledgement-rate model** that predicts, for the here-and-now context, how
likely the next coach nudge is to be acknowledged, so the engine can hold a
non-critical cue *before* the user has to ignore three in a row.

Design constraints (mirroring the rest of Prefrontal's learning loop):

- **Local-first, no new deps, pure.** The model is an empirical-Bayes ack-rate
  estimator over cheap context features already available at tick time — a coarse
  local-hour bucket, weekday/weekend, the delivery channel class, and recent
  dosage (how many nudges already fired today). Each context *cell*'s ack-rate is
  shrunk toward the user's pooled rate by a smoothing weight, so a sparsely-seen
  cell defaults to the pooled behaviour rather than to a jumpy 0/1. Deterministic:
  no ``Date.now()`` / ``random`` in the core — every time-derived value is threaded
  in by the caller, and the estimator is closed-form (no sampling), so a test that
  fits the same episodes always gets the same prediction.
- **Trains on the episodes that already exist.** The features and label all come
  off the ``coach nudge:`` channel-outcome ``reminder`` episodes the learning loop
  logs today (:func:`prefrontal.coaching.record_channel_outcome`): ``timestamp`` →
  hour/day-of-week, ``channel`` → channel class, ``acknowledged`` → the label,
  ``context`` → the coach-nudge filter. No new episode schema.
- **Off until earned (the honesty gate).** :func:`receptivity_calibration` is the
  walk-forward twin of :func:`prefrontal.memory.patterns.bias_calibration` /
  ``channel_calibration``: learn the model on the *older* slice, then check on the
  *newer* held-out slice whether conditioning on context predicts acknowledgement
  **better than the pooled baseline**. The engine only lets this model gate once
  that check says it *helps* on that user's own history; until then the rules gate
  stands. A bad adaptation is visible and inert, never silently asserted.

This module is the **pure core** (feature extraction, the estimator, the
walk-forward check). The store reads, the on/off state, and the wiring into
``decide`` live in :mod:`prefrontal.coaching`; the calibration verdict is persisted
by the learning pass (:func:`prefrontal.memory.patterns.recompute_patterns`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean
from typing import Any

from prefrontal.clock import local_datetime
from prefrontal.clock import parse_ts as _parse_ts

#: Prefix the channel-outcome loop stamps on a coach nudge's episode ``context``
#: (see :func:`prefrontal.coaching.record_channel_outcome`). The single source of
#: truth for the wire format, imported by the coaching engine's receptivity read so
#: the writer, the rules gate, and this learned model can't drift apart.
COACH_NUDGE_CONTEXT_PREFIX = "coach nudge:"

#: Width (hours) of a local-hour feature bucket. Six buckets a day (0–3, 4–7, …):
#: coarse enough that each cell fills on modest history, fine enough to separate
#: "reachable mid-morning" from "never at 2am".
HOUR_BUCKET_HOURS = 4

#: Recent-dosage buckets are clamped to this ceiling (0, 1, 2, 3+), so "already
#: fired a lot today" collapses to one cell instead of scattering thin tails.
DOSAGE_BUCKET_CAP = 3

#: Empirical-Bayes smoothing weight: a context cell's own ack-rate is blended with
#: the pooled rate as ``(hits + α·pooled) / (n + α)``. α≈4 means a cell needs about
#: four observations to move halfway from the pooled prior to its own rate — "trust
#: the cell only once it has said something", the same shrinkage spirit as the
#: patterns module's confidence estimator.
DEFAULT_SHRINKAGE_ALPHA = 4.0

#: Minimum usable observations before the walk-forward check even attempts a split
#: (needs a train slice meeting :data:`MIN_MODEL_TRAIN` plus a test slice). Mirrors
#: ``patterns.MIN_CALIBRATION_SAMPLES``.
MIN_CALIBRATION_SAMPLES = 6

#: Minimum train-slice observations before the fitted model is trustworthy enough
#: to score. Mirrors ``patterns.MIN_BIAS_SAMPLES``.
MIN_MODEL_TRAIN = 3

#: Fraction of (time-ordered) observations held out as the recent test slice.
#: Mirrors ``patterns.DEFAULT_CALIBRATION_TEST_FRACTION``.
DEFAULT_TEST_FRACTION = 0.34

#: A context cell: coarse hour bucket, weekday/weekend class, channel class, and
#: clamped recent-dosage bucket. The unit the ack-rate is conditioned on.
Cell = tuple[int, str, str, int]


@dataclass
class _Tally:
    """Mutable per-cell accumulator used while fitting: acks seen and trials seen.

    ``hits`` is a float because an observation's ack is ``1.0``/``0.0`` (so partials
    stay expressible); ``trials`` is an integer count. Kept distinct so the fit loop
    reads as what it is, rather than indexing a ``list[float]`` of mixed meaning.
    """

    hits: float = 0.0
    trials: int = 0


def context_cell(local_dt: datetime, channel: str, dosage: int) -> Cell:
    """Build the context :data:`Cell` a nudge would land in.

    Pure and total: every input is threaded in (the local datetime already
    resolved for the user's timezone, the engine channel class, the recent-dosage
    count), so the same inputs always map to the same cell — in training and at
    prediction time alike. Buckets are deliberately coarse (see the module
    constants) to keep cells populated on the sparse histories this population
    generates.
    """
    hour_bucket = local_dt.hour // HOUR_BUCKET_HOURS
    dow = "we" if local_dt.weekday() >= 5 else "wk"
    dosage_bucket = min(max(int(dosage), 0), DOSAGE_BUCKET_CAP)
    return (hour_bucket, dow, channel or "", dosage_bucket)


@dataclass(frozen=True)
class ReceptivityModel:
    """A per-user contextual acknowledgement-rate estimator (empirical Bayes).

    Holds the user's pooled ack-rate and, per context :data:`Cell`, the (hits,
    trials) seen. :meth:`predict` returns the shrunk ack-rate for a cell — its own
    rate pulled toward ``pooled`` by :data:`DEFAULT_SHRINKAGE_ALPHA`, so an unseen
    or barely-seen cell reads as "behaves like this user on average" rather than a
    confident 0 or 1. Transparent and closed-form: no sampling, no hidden state.
    """

    pooled: float
    alpha: float = DEFAULT_SHRINKAGE_ALPHA
    #: cell -> (hits, trials)
    cells: dict[Cell, tuple[float, int]] = field(default_factory=dict)

    def predict(self, cell: Cell) -> float:
        """The shrunk acknowledgement probability ∈ [0, 1] for ``cell``."""
        hits, n = self.cells.get(cell, (0.0, 0))
        return (hits + self.alpha * self.pooled) / (n + self.alpha)

    @classmethod
    def fit(
        cls,
        observations: Iterable[tuple[Cell, float]],
        *,
        alpha: float = DEFAULT_SHRINKAGE_ALPHA,
    ) -> ReceptivityModel:
        """Fit from ``(cell, ack)`` pairs (``ack`` is 1.0 acknowledged / 0.0 ignored).

        Returns a model whose ``pooled`` is the overall ack-rate (0.0 on no data);
        callers that get an empty ``observations`` should not gate on the result
        (there is nothing to condition on) — :mod:`prefrontal.coaching` treats a
        no-data fit as "fall back to the rules gate".
        """
        cells: dict[Cell, _Tally] = defaultdict(_Tally)
        total_hits = 0.0
        total_n = 0
        for cell, ack in observations:
            tally = cells[cell]
            tally.hits += ack
            tally.trials += 1
            total_hits += ack
            total_n += 1
        pooled = total_hits / total_n if total_n else 0.0
        return cls(
            pooled=pooled,
            alpha=alpha,
            cells={c: (t.hits, t.trials) for c, t in cells.items()},
        )

    @property
    def trials(self) -> int:
        """Total observations the model was fit on (0 ⇒ nothing to condition on)."""
        return sum(n for _, n in self.cells.values())


def observations_from_episodes(
    episodes: list[dict[str, Any]], timezone: str = "UTC"
) -> list[tuple[datetime, Cell, float]]:
    """Turn raw episodes into time-ordered ``(timestamp, cell, ack)`` observations.

    Keeps only the ``coach nudge:`` channel-outcome ``reminder`` episodes that carry
    both a delivery ``channel`` and a resolved ``acknowledged`` (the ones
    :func:`prefrontal.coaching.record_channel_outcome` writes), and derives each
    observation's context :data:`Cell` from the episode alone:

    - local hour bucket + weekday/weekend from ``timestamp`` (in ``timezone``);
    - channel class from ``channel``;
    - recent-dosage bucket from **how many coach nudges already went out earlier the
      same local day** — the historical reconstruction of the tick-time "dosage so
      far today" signal, computed by walking the episodes in time order.

    Returned sorted ascending by timestamp, so a caller can split it walk-forward.
    """
    rows: list[tuple[datetime, str, float]] = []
    for e in episodes:
        if e.get("episode_type") != "reminder":
            continue
        if not str(e.get("context") or "").startswith(COACH_NUDGE_CONTEXT_PREFIX):
            continue
        channel = e.get("channel")
        acknowledged = e.get("acknowledged")
        if not channel or acknowledged is None:
            continue
        ts = _parse_ts(e.get("timestamp"))
        if ts is None:
            continue
        rows.append((ts, str(channel), 1.0 if acknowledged else 0.0))
    rows.sort(key=lambda r: r[0])

    out: list[tuple[datetime, Cell, float]] = []
    day_counts: dict[Any, int] = {}
    for ts, channel, ack in rows:
        ldt = local_datetime(ts, timezone)
        day = ldt.date()
        dosage = day_counts.get(day, 0)
        out.append((ts, context_cell(ldt, channel, dosage), ack))
        day_counts[day] = dosage + 1
    return out


@dataclass(frozen=True)
class ReceptivityCalibration:
    """Does conditioning on context predict acknowledgement better than pooled? (§4)

    The walk-forward honesty gate for the learned receptivity model, the exact twin
    of :class:`prefrontal.memory.patterns.ChannelCalibration`: fit the model on the
    older *train* slice, then on the newer *test* slice compare the mean absolute
    error of predicting each ack with its context cell's shrunk rate (``adjusted``)
    versus with a single pooled train rate (``baseline``, i.e. no conditioning).
    ``improvement`` = baseline − adjusted (positive ⇒ context helped); ``helps`` is
    the verdict the engine reads before it will let the model gate.
    """

    status: str  # "ok" | "insufficient"
    samples: int  # test-slice observations the error was measured on
    baseline_error: float | None = None
    adjusted_error: float | None = None
    improvement: float | None = None
    helps: bool = False


def receptivity_calibration(
    episodes: list[dict[str, Any]],
    *,
    timezone: str = "UTC",
    min_samples: int = MIN_CALIBRATION_SAMPLES,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    alpha: float = DEFAULT_SHRINKAGE_ALPHA,
) -> ReceptivityCalibration:
    """Walk-forward check on the learned receptivity model (honest, not circular).

    Sorts the coach-nudge observations by time, learns the contextual model from
    the older *train* slice, then compares held-out prediction error on the newer
    *test* slice: the pooled train rate (``baseline_error``) versus the model's
    per-cell shrunk rate (``adjusted_error``). ``helps`` iff conditioning on context
    predicted the held-out acks strictly better.

    Returns ``status="insufficient"`` (never raises) when there aren't enough
    observations to split — so the model stays dormant on sparse data, which is the
    correct and expected behaviour, not a bug.
    """
    obs = observations_from_episodes(episodes, timezone)
    n = len(obs)
    if n < min_samples:
        return ReceptivityCalibration(status="insufficient", samples=n)
    test_size = max(2, round(n * test_fraction))
    train, test = obs[: n - test_size], obs[n - test_size :]
    if len(train) < MIN_MODEL_TRAIN:
        return ReceptivityCalibration(status="insufficient", samples=n)

    model = ReceptivityModel.fit(((cell, ack) for _, cell, ack in train), alpha=alpha)
    pooled = model.pooled
    baseline_error = fmean(abs(pooled - ack) for _, _, ack in test)
    adjusted_error = fmean(abs(model.predict(cell) - ack) for _, cell, ack in test)
    improvement = baseline_error - adjusted_error
    return ReceptivityCalibration(
        status="ok",
        samples=len(test),
        baseline_error=round(baseline_error, 3),
        adjusted_error=round(adjusted_error, 3),
        improvement=round(improvement, 3),
        helps=improvement > 0,
    )
