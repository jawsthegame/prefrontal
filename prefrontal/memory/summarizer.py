"""Compress the memory tables into a behavioral profile document.

The README describes a summarizer agent that periodically reads ``patterns`` and
``coaching_state`` and writes a ``profile.md`` that every agent prepends to its
system prompt — so behavioral context travels with every interaction.

There are two layers here:

- :func:`build_profile` — the **deterministic, structured** profile: the facts,
  rendered from the tables with simple templates and no model call. Testable,
  dependency-free, and stable enough to diff.
- :func:`summarize_profile` — the **LLM-backed** summarizer: it feeds the
  structured profile to a local Ollama model and asks for concise, prioritized,
  second-person coaching guidance (the ``docs/schema.md`` example). If the model
  is unavailable or errors, it falls back to the structured profile, so the
  pipeline never hard-fails on a down model.

The structured profile is the model's *input*, which keeps the LLM grounded in
real numbers rather than free-associating.

Because the LLM pass needs a (slow) model round-trip, the narrative is **cached**
in the ``profile_cache`` table rather than regenerated per request:
:func:`refresh_profile_cache` writes it (nightly, via ``prefrontal summarize``)
and :func:`load_cached_summary` reads it back, so ``GET /profile`` serves prose
without hitting the model on every poll. :func:`cache_is_stale` flags when the
underlying facts have moved on since the cache was written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prefrontal.memory.store import MemoryStore

if TYPE_CHECKING:
    from prefrontal.integrations.ollama import OllamaClient
    from prefrontal.modules.base import Module

#: System prompt steering the model from structured facts to coaching prose.
SUMMARIZER_SYSTEM_PROMPT = (
    "You are Prefrontal's profile summarizer. You are given a structured "
    "behavioral profile of one person with ADHD: learned patterns, coaching "
    "preferences, and active support modules. Rewrite it as concise, prioritized "
    "coaching guidance addressed to the agents that will help this person — "
    "second person ('the user'), 4-6 sentences, most actionable first. Obey the "
    "numbers exactly, especially any time-estimation multiplier and responsive "
    "hours. Do not invent facts not present in the input. Lead with what to do, "
    "not a description. No preamble, headings, or bullet list — just the prose."
)


def build_profile(
    store: MemoryStore, modules: list[Module] | None = None
) -> str:
    """Render a Markdown behavioral profile from the current memory state.

    The output is intentionally plain and stable so it can be diffed, tested,
    and injected verbatim into agent prompts. It has three parts: derived
    patterns (highest confidence first), current coaching preferences, and one
    section per enabled challenge-area module.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        modules: Modules whose sections to include. Defaults to the configured
            set from :func:`prefrontal.modules.enabled_modules`. Pass an empty
            list to omit module sections entirely.

    Returns:
        A Markdown string suitable for writing to ``profile.md`` and prepending
        to an agent system prompt.
    """
    if modules is None:
        # Imported lazily to keep the memory layer free of a hard dependency on
        # the modules package (and to avoid import cycles).
        from prefrontal.modules import enabled_modules

        modules = enabled_modules()

    lines: list[str] = ["# Behavioral profile", ""]

    state = store.all_state()
    patterns = store.get_patterns()

    lines.append("## Learned patterns")
    lines.append("")
    if not patterns:
        lines.append(
            "_No patterns derived yet — not enough episodes have accumulated._"
        )
    else:
        for p in patterns:
            lines.append("- " + _describe_pattern(p))
    lines.append("")

    lines.append("## Coaching preferences")
    lines.append("")
    if not state:
        lines.append("_No coaching state recorded._")
    else:
        for key, row in state.items():
            source = row.get("source") or "unknown"
            lines.append(f"- **{key}**: {row.get('value')} _(source: {source})_")
    lines.append("")

    # Surface the time-estimation bias as an explicit instruction, since it is
    # the canonical example from the schema docs and the most actionable signal.
    bias = state.get("time_estimation_bias", {}).get("value") if state else None
    if bias:
        try:
            pct = round((float(bias) - 1.0) * 100)
        except ValueError:
            pct = None
        if pct is not None:
            lines.append(
                f"> Apply a {bias}x multiplier to time estimates "
                f"(historical ~{pct}% underestimate)."
            )
            # Whether that multiplier is actually earning its keep (learning §4):
            # a walk-forward check writes this verdict on each learn pass.
            helps = state.get("bias_calibration_helps", {}).get("value") if state else None
            improvement = (
                state.get("bias_calibration_improvement", {}).get("value") if state else None
            )
            if helps == "true":
                extra = f" (~{improvement} closer)" if improvement else ""
                lines.append(f"> This multiplier is improving recent estimates{extra} — trust it.")
            elif helps == "false":
                lines.append(
                    "> ⚠️ This multiplier is NOT improving recent estimates — treat it "
                    "cautiously; a reset toward 1.0 may be due."
                )
            # Context-conditioned by time of day (learning §5), where there's
            # enough signal — so estimates are calibrated to *when* they run.
            bands = [
                (b, state.get(f"time_estimation_bias:{b}", {}).get("value"))
                for b in ("morning", "afternoon", "evening")
            ]
            banded = [f"{b} {v}x" for b, v in bands if v]
            if banded:
                lines.append(f"> By time of day: {', '.join(banded)}.")
            lines.append("")

    # One section per enabled challenge-area module. Modules that have nothing
    # to say (return None/empty) are skipped so the profile stays tight.
    if modules:
        lines.append("## Active modules")
        lines.append("")
        for module in modules:
            section = module.profile_section(store)
            lines.append(f"### {module.title}")
            lines.append("")
            lines.append(section if section else "_No signal yet._")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class ProfileSummary:
    """Result of :func:`summarize_profile`.

    Attributes:
        text: The profile text to inject into agent prompts.
        source: ``"llm"`` if a model produced it, ``"heuristic"`` if the
            structured fallback was used.
        model: The model name when ``source == "llm"``, else ``None``.
        structured: The structured profile that was fed to the model (also the
            fallback text), useful for debugging and diffing.
        generated_at: When the narrative was produced — set only when the
            summary was loaded from the cache (see :func:`load_cached_summary`);
            ``None`` for a freshly generated one.
    """

    text: str
    source: str
    model: str | None = None
    structured: str = ""
    generated_at: str | None = None


def summarize_profile(
    store: MemoryStore,
    *,
    client: OllamaClient | None = None,
    modules: list[Module] | None = None,
    fallback: bool = True,
) -> ProfileSummary:
    """Produce the behavioral profile, preferring an LLM narrative.

    Builds the structured profile (:func:`build_profile`), then asks a local
    Ollama model to rewrite it as prioritized coaching prose. On any model
    failure it returns the structured profile instead (unless ``fallback`` is
    ``False``), so a down model never breaks the pipeline.

    Args:
        store: An open :class:`~prefrontal.memory.store.MemoryStore`.
        client: An Ollama client. Defaults to one built from settings.
        modules: Modules to include in the structured profile (see
            :func:`build_profile`).
        fallback: If ``True`` (default), fall back to the structured profile on
            model failure; if ``False``, re-raise the error.

    Returns:
        A :class:`ProfileSummary`.

    Raises:
        prefrontal.integrations.ollama.OllamaError: If the model fails and
            ``fallback`` is ``False``.
    """
    from prefrontal.integrations.ollama import OllamaClient, OllamaError

    structured = build_profile(store, modules=modules)
    client = client or OllamaClient.from_settings()

    try:
        prose = client.generate(structured, system=SUMMARIZER_SYSTEM_PROMPT)
    except OllamaError:
        if not fallback:
            raise
        return ProfileSummary(
            text=structured, source="heuristic", model=None, structured=structured
        )

    # An empty (or whitespace-only) model reply is treated as a failure to
    # produce anything useful.
    prose = prose.strip()
    if not prose:
        if not fallback:
            raise OllamaError("Ollama returned an empty summary.")
        return ProfileSummary(
            text=structured, source="heuristic", model=None, structured=structured
        )

    return ProfileSummary(
        text=prose, source="llm", model=client.model, structured=structured
    )


def cache_summary(store: MemoryStore, summary: ProfileSummary) -> None:
    """Persist a :class:`ProfileSummary` to the single-row profile cache.

    This is what makes the (slow) LLM narrative cheap to serve: the nightly
    ``prefrontal summarize`` writes it once and ``GET /profile`` reads it back
    via :func:`load_cached_summary` on every poll.
    """
    store.set_profile_cache(
        summary.text,
        source=summary.source,
        model=summary.model,
        structured=summary.structured,
    )


def load_cached_summary(store: MemoryStore) -> ProfileSummary | None:
    """Return the cached narrative as a :class:`ProfileSummary`, or ``None``.

    The returned summary carries ``generated_at`` so callers can report (and
    age-check) when the narrative was produced.
    """
    row = store.get_profile_cache()
    if row is None:
        return None
    return ProfileSummary(
        text=row["text"],
        source=row["source"],
        model=row["model"],
        structured=row.get("structured") or "",
        generated_at=row.get("generated_at"),
    )


def refresh_profile_cache(
    store: MemoryStore,
    *,
    client: OllamaClient | None = None,
    modules: list[Module] | None = None,
    fallback: bool = True,
) -> ProfileSummary:
    """Regenerate the narrative via :func:`summarize_profile` and cache it.

    Returns the freshly generated :class:`ProfileSummary` (also now persisted).
    Used by ``prefrontal summarize`` and by ``GET /profile?refresh=1``.
    """
    summary = summarize_profile(
        store, client=client, modules=modules, fallback=fallback
    )
    cache_summary(store, summary)
    return summary


def cache_is_stale(
    store: MemoryStore,
    cached: ProfileSummary | None = None,
    *,
    modules: list[Module] | None = None,
) -> bool:
    """Report whether the cached narrative no longer matches the current facts.

    Rebuilds the structured profile and compares it to the one the cached prose
    was derived from. A missing cache, or one with no recorded structured input,
    counts as stale (there is nothing to trust).

    Args:
        store: An open store.
        cached: A previously loaded summary; loaded on demand when omitted.
        modules: Modules to include in the structured profile (must match what
            the cache was built with for the comparison to be meaningful).
    """
    cached = cached or load_cached_summary(store)
    if cached is None or not cached.structured:
        return True
    return build_profile(store, modules=modules) != cached.structured


def _describe_pattern(pattern: dict) -> str:
    """Build a one-line human description of a single pattern row.

    Args:
        pattern: A pattern dict as returned by
            :meth:`~prefrontal.memory.store.MemoryStore.get_patterns`.

    Returns:
        A single-line summary including the confidence and sample size.
    """
    ptype = pattern.get("pattern_type", "pattern")
    ckey = pattern.get("context_key", "?")
    observed = pattern.get("observed_value")
    predicted = pattern.get("predicted_value")
    variance = pattern.get("variance")
    confidence = pattern.get("confidence") or 0.0
    samples = pattern.get("sample_size") or 0

    detail_parts: list[str] = []
    if predicted is not None and observed is not None:
        detail_parts.append(f"predicted {predicted}, observed {observed}")
        if variance is not None:
            direction = "under" if variance > 0 else "over"
            detail_parts.append(f"{direction}estimate by {abs(variance)}")
    elif observed is not None:
        # Rate-style patterns (channel_response, drift) have only an observed value.
        detail_parts.append(f"observed {observed}")
    detail = "; ".join(detail_parts) if detail_parts else "no comparison data"

    return (
        f"**{ptype}** for `{ckey}`: {detail} "
        f"(confidence {confidence:.0%}, n={samples})"
    )
