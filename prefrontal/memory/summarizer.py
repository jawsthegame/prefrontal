"""Compress the memory tables into a behavioral profile document.

The README describes a summarizer agent that periodically reads ``patterns`` and
``coaching_state`` and writes a ``profile.md`` that every agent prepends to its
system prompt — so behavioral context travels with every interaction.

This module ships the **deterministic, heuristic** version of that summarizer.
It turns the structured rows into readable prose using simple templates and no
model call, which makes it testable and dependency-free. The richer LLM-backed
summarizer (which would synthesize nuanced, prioritized guidance) is a planned
follow-up.

.. todo::
   Replace :func:`build_profile` (or add a sibling) that calls a local Ollama
   model — or optionally the Anthropic API — to produce the narrative profile,
   using this heuristic output as a structured fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prefrontal.memory.store import MemoryStore

if TYPE_CHECKING:
    from prefrontal.modules.base import Module


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
    detail = "; ".join(detail_parts) if detail_parts else "no comparison data"

    return (
        f"**{ptype}** for `{ckey}`: {detail} "
        f"(confidence {confidence:.0%}, n={samples})"
    )
