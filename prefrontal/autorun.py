"""Bounded agentic runs for a delegated todo — "auto mode" (roadmap M4, phase 1).

Delegation's ``agent`` handler does *one* generation: the model reads the todo (and
any pasted context) and writes back a brief. It never looks anything up. This module
is the next rung — a **bounded research loop**: the model may call a small set of
allowlisted MCP tools, one at a time, and what it learns is folded into the prep it
hands back. See ``docs/design/auto-mode-delegation.md``.

The whole design is about staying inside M4's guardrail — *scoped, verifiable,
API/MCP-based actions, never an open-ended computer-use agent*. The bound that
matters is **which tools exist**, not how many turns we take:

1. **Two gates, not one.** A tool is callable here only if it is on its server's
   ``allowed_tools`` (:mod:`prefrontal.actions`' existing confirm-gated allowlist)
   *and* on its ``unattended_tools`` — the operator's explicit declaration that it
   is safe to fire with nobody watching. Both empty by default, so enabling MCP
   grants no unattended autonomy at all. Phase 1 ships **no** effectful tools:
   delivery (email the result, drop a file in Drive) is phase 2 and arrives as a
   *pre-authorized delivery contract*, not as a tool the loop may pick.
2. **Hard budgets.** A step cap, a wall-clock deadline, and a per-observation
   character cap. Exhausting a budget is a normal outcome that keeps its partial
   findings — never an error, never a silent truncation (the trail says what stopped
   it).
3. **A recorded trail.** Every step is returned (and persisted on the delegation
   row) so a run that dies at step 4 is inspectable rather than vanished. This is
   *in addition to* the inert per-call ``action`` audit episode that
   :func:`prefrontal.actions.run_action` already writes.

**Asking is a move, not an error path.** Plenty of useful tasks can't be finished
without facts only the user has — "should I get a HELOC for the remodel?" needs their
equity, rate, and risk tolerance, and no tool will supply those. So alongside
``call`` and ``done`` the loop may answer ``ask`` with a short list of questions; the
caller then parks the todo in ``needs_input`` with whatever the run *did* work out
already on it, and folds the answers back in (via :func:`answered_context`) when they
arrive. Answering **re-runs** the loop rather than resuming a suspended one: there is
no paused interpreter to revive and no session to expire, a re-run with better inputs
is free to take a different path than the blind alley it stopped in, and the state
that has to survive is just data. Answers can therefore arrive minutes or days later,
inline or in prose, and nothing here has to care.

**One JSON turn at a time, deliberately.** The loop asks the model for a single
``{"action": …}`` object per turn instead of using a provider's native tool-calling
API. That keeps it inside the existing :class:`~prefrontal.integrations.Generator`
protocol — so either provider works today with no new client capability — and it
reuses the house pattern of tolerant JSON extraction plus an honest fallback. Native
tool-use can replace :func:`_next_turn` later without changing anything else here.

Nothing in this module raises on a model or tool failure: a dead model, an
unreachable server, or a garbage reply all end the loop early and leave the findings
gathered so far, because the caller's contract is "always come back with something
useful."
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from prefrontal.clock import utcnow
from prefrontal.integrations import Generator
from prefrontal.integrations.base import ProviderError
from prefrontal.llm_json import extract_json_object, fit_num_ctx, generate_text
from prefrontal.log import get_logger

if TYPE_CHECKING:
    from prefrontal.config import Settings
    from prefrontal.memory.store import MemoryStore

logger = get_logger(__name__)

#: Tool calls one run may make. Small on purpose: a research loop that hasn't found
#: its footing in half a dozen lookups won't find it in twenty, and every extra step
#: is local-model minutes the user is waiting through.
DEFAULT_MAX_STEPS = 6

#: Wall-clock ceiling for a whole run (seconds). Checked *before* each turn, so a
#: long final generation can overrun it rather than being thrown away half-done.
DEFAULT_DEADLINE_SECONDS = 600.0

#: Most characters of a single tool result carried forward into the next prompt.
#: A tool that returns a whole web page would otherwise blow out the context window
#: (and, on Ollama, get silently truncated from the front — see PR #438).
DEFAULT_OBSERVATION_CHARS = 2000

#: Largest context window to request for a loop turn, and the per-turn timeout. The
#: prompt grows with each observation, so this sizes up like the prep call does.
_TURN_MAX_NUM_CTX = 16384
_TURN_TIMEOUT = 120.0

#: The most questions one run may ask. A handful is a conversation; twenty is a
#: form, and a form is the thing "activation energy → zero" exists to prevent. The
#: cap is also what stops a confused model turning a run into an interrogation.
MAX_QUESTIONS = 5

_LOOP_SYSTEM = (
    "You are an executive assistant doing the legwork on one task, using tools. "
    "Each turn, reply with ONLY a JSON object — one of: "
    '{"action": "call", "tool": "<server.tool>", "arguments": {...}, "why": "<one '
    'short line>"} to use a tool; '
    '{"action": "ask", "questions": [{"text": "<question>", "why": "<why you need '
    'it>"}]} when the task cannot be finished without facts only the user has; '
    'or {"action": "done", "why": "<one short line>"} when you have enough to write '
    "up the task. "
    "Rules: call ONE tool per turn, chosen from the listed tools only (use the exact "
    '"server.tool" name). Never invent a tool. Do not repeat a call you already made '
    "— read the observation you got and build on it. Stop as soon as you have enough; "
    'answering "done" early is better than padding. You cannot send messages, write '
    "files, or contact anyone — only gather information. "
    f'Only "ask" for things you genuinely cannot look up (the user\'s own numbers, '
    f"preferences, or constraints) — never for something a tool could tell you, and "
    f"never more than {MAX_QUESTIONS} questions. Do the lookups you can do first, so "
    "your questions are the short list that's actually left. "
    'Write each question TO the user, in the second person ("What\'s your current '
    'mortgage rate?", not "What is my current mortgage rate?"), and write its "why" '
    'as what it unblocks ("it decides whether refinancing beats a HELOC").'
)

#: Appended once the user has already answered a round of questions. Without it a
#: model will happily ask a fresh question every round and never conclude — the
#: ping-pong that would make auto mode feel like an intake form.
_FINISH_UP = (
    " You have ALREADY asked the user questions and they answered (see the context). "
    'Do not ask again unless the task is genuinely impossible without it — finish and '
    'answer "done", stating your conclusion with the assumptions you had to make.'
)


@dataclass(frozen=True)
class RunBudget:
    """The hard limits on one run.

    Attributes:
        max_steps: Tool calls allowed (the loop also stops when the model says done).
        deadline_seconds: Wall-clock ceiling, checked before each turn.
        observation_chars: Per-result characters carried into the next prompt.
    """

    max_steps: int = DEFAULT_MAX_STEPS
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS
    observation_chars: int = DEFAULT_OBSERVATION_CHARS


@dataclass(frozen=True)
class RunStep:
    """One executed tool call — the persisted, inspectable trail of a run.

    ``ok`` is the tool's own verdict; ``observation`` is the (truncated) content it
    returned. ``why`` is the model's one-line reason for the call, kept because a
    trail of *why* reads far better than a trail of arguments when you're working out
    what a run actually did.
    """

    index: int
    server: str
    tool: str
    arguments: dict[str, Any]
    ok: bool
    detail: str = ""
    observation: str = ""
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable form (what lands in ``todo_delegations.steps``)."""
        return {
            "index": self.index,
            "server": self.server,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "detail": self.detail,
            "observation": self.observation,
            "why": self.why,
        }


@dataclass(frozen=True)
class RunQuestion:
    """One thing the run needs from the user before it can finish.

    ``why`` is required in spirit (the model is told to supply it) because a bare
    question reads as an interrogation, while "what's your current mortgage rate? —
    it decides whether refinancing beats a HELOC" reads as work being done. ``answer``
    is filled in when the user replies; the pair is what a re-run gets as context.
    """

    text: str
    why: str = ""
    answer: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-serializable form (what lands in ``todo_delegations.questions``)."""
        return {"text": self.text, "why": self.why, "answer": self.answer}


@dataclass(frozen=True)
class RunResult:
    """What a run gathered, and why it stopped.

    Attributes:
        steps: The executed calls, in order (possibly empty).
        findings: The gathered material as text, ready to append to the prep
            context. Empty when nothing was learned.
        questions: What the run needs from the user (only when ``stop_reason`` is
            ``needs_input``).
        stop_reason: ``done`` (the model finished), ``needs_input`` (it asked the
            user something), ``budget`` (step cap), ``deadline``, ``no_tools``,
            ``model_unavailable``, or ``stalled`` (the model stopped producing usable
            turns).
        detail: A short human-readable summary of the run.
    """

    steps: list[RunStep] = field(default_factory=list)
    findings: str = ""
    questions: list[RunQuestion] = field(default_factory=list)
    stop_reason: str = "no_tools"
    detail: str = ""

    @property
    def calls(self) -> int:
        """How many tool calls actually ran."""
        return len(self.steps)


@dataclass(frozen=True)
class Toolbox:
    """The tools a run may use, plus the bound function that calls one.

    Kept as an injected value object (rather than autorun reaching for the store and
    settings itself) so the loop is a pure function of its inputs and a test can hand
    it two fake tools without an MCP server or a database.

    Attributes:
        tools: Callable tools, each ``{server, tool, description, input_schema}``.
        call: ``(server, tool, arguments) -> (ok, content, detail)``. Must never
            raise.
    """

    tools: list[dict[str, Any]] = field(default_factory=list)
    call: Callable[[str, str, dict[str, Any]], tuple[bool, str, str]] | None = None

    def names(self) -> set[str]:
        """The addressable ``server.tool`` names."""
        return {f"{t['server']}.{t['tool']}" for t in self.tools}


def build_toolbox(
    store: MemoryStore,
    settings: Settings,
    *,
    client_factory: Any = None,
) -> Toolbox:
    """Assemble the unattended toolbox from the configured MCP servers.

    The narrowing happens here, once: a tool is included only if its server
    advertises it, it is on ``allowed_tools`` (enforced again inside
    :func:`~prefrontal.actions.run_action`), *and* it is on ``unattended_tools``. So a
    deployment with no ``unattended_tools`` gets an empty toolbox and auto mode
    degrades to today's single-shot prep.

    Args:
        store: The user-scoped store (used only for the per-call audit episode).
        settings: Resolved settings, for the MCP server config.
        client_factory: Optional MCP client factory, for tests.

    Returns:
        A :class:`Toolbox`; empty when nothing is declared unattended.
    """
    from prefrontal import actions

    unattended = {
        server.name: server.unattended_tools
        for server in settings.mcp_servers
        if server.unattended_tools
    }
    if not unattended:
        return Toolbox()
    tools = [
        t
        for t in actions.list_available_tools(settings, client_factory=client_factory)
        if t["tool"] in unattended.get(t["server"], frozenset())
    ]

    def _call(server: str, tool: str, arguments: dict[str, Any]) -> tuple[bool, str, str]:
        # Belt and braces: the loop is told which tools exist, but a model that
        # names one anyway must not reach a server. `run_action` re-checks
        # `allowed_tools`; this re-checks the narrower unattended set.
        if tool not in unattended.get(server, frozenset()):
            return False, "", f'"{server}.{tool}" is not allowed to run unattended'
        outcome = actions.run_action(
            store,
            settings,
            server,
            tool,
            arguments,
            # An internal caller: there was no preview to drift from, and the
            # allowlist (not a digest) is what bounds this call.
            expected_digest=None,
            client_factory=client_factory,
        )
        return outcome.ran, outcome.content, outcome.detail

    return Toolbox(tools=tools, call=_call)


def _catalog(toolbox: Toolbox) -> str:
    """Render the available tools for the prompt (name, description, arguments)."""
    lines = []
    for t in toolbox.tools:
        line = f"- {t['server']}.{t['tool']}"
        if t.get("description"):
            line += f": {t['description']}"
        props = (t.get("input_schema") or {}).get("properties")
        if isinstance(props, dict) and props:
            line += f"\n    arguments: {', '.join(sorted(props))}"
        lines.append(line)
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    """Cap a tool result, saying so — a silent truncation reads as a full answer."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"… [truncated, {len(text) - limit} more characters]"


def _next_turn(
    client: Generator,
    task: str,
    catalog: str,
    transcript: list[str],
    remaining: int,
    *,
    system: str = _LOOP_SYSTEM,
) -> dict[str, Any] | None:
    """Ask the model for its next move; ``None`` if it didn't produce a usable one.

    One completion, tolerant JSON extraction, no raising — the same shape as every
    other model call site in the codebase. The prompt grows with the transcript, so
    the context window is sized to fit (an under-sized window truncates from the
    *front*, which would silently drop the task itself).
    """
    prompt = (
        f"{task}\n\nTools available:\n{catalog}\n\n"
        + ("Work so far:\n" + "\n".join(transcript) + "\n\n" if transcript else "")
        + f"You may make at most {remaining} more tool call"
        + ("s" if remaining != 1 else "")
        + ". What is your next move?"
    )
    num_ctx = fit_num_ctx(len(prompt) + len(system), cap=_TURN_MAX_NUM_CTX)
    try:
        reply = generate_text(
            client,
            prompt,
            system=system,
            num_ctx=num_ctx,
            timeout=_TURN_TIMEOUT,
            want_json=True,
        )
    except ProviderError as exc:  # Ollama/Anthropic transport or model failure
        logger.info("auto-run turn failed: %s", exc)
        return None
    raw = extract_json_object(reply)
    return raw or None


def _coerce_questions(raw: Any) -> list[RunQuestion]:
    """Keep only well-formed, de-duplicated questions from a model reply (capped).

    Defensive in the house style: a model that answers with bare strings, repeats
    itself, or asks fifteen things still yields a usable short list rather than an
    exception or a form.
    """
    if not isinstance(raw, list):
        return []
    out: list[RunQuestion] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            text, why = item.strip(), ""
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("question") or "").strip()
            why = str(item.get("why") or "").strip()
        else:
            continue
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(RunQuestion(text=text[:500], why=why[:300]))
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def answered_context(questions: list[dict[str, Any]] | None) -> str:
    """Render answered questions as context for a re-run, or ``""``.

    The durable half of the round-trip: a re-run gets the Q&A as plain facts, which
    is why answering can restart the loop instead of resuming a suspended one.
    Unanswered questions are skipped — they'd only invite the model to answer its own
    question with an invented value.
    """
    pairs = [
        (str(q.get("text", "")).strip(), str(q.get("answer") or "").strip())
        for q in (questions or [])
        if isinstance(q, dict)
    ]
    pairs = [(t, a) for t, a in pairs if t and a]
    if not pairs:
        return ""
    lines = ["You asked the user these questions and they answered:"]
    for text, answer in pairs:
        lines.append(f"Q: {text}\nA: {answer}")
    return "\n".join(lines)


def merge_questions(
    prior: list[dict[str, Any]] | None, fresh: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """The question list to persist after a run: answered history, then new asks.

    Answered questions are kept (they're the record of what the user told it, and the
    UI shows them alongside whatever is still outstanding); *unanswered* ones from the
    previous round are dropped, because this run either got them answered or moved on
    and re-asking a superseded question is just noise. A fresh question that repeats an
    answered one is dropped too — that's a model forgetting what it was told, and the
    user shouldn't have to type it twice.
    """
    kept = [
        q for q in (prior or []) if isinstance(q, dict) and str(q.get("answer") or "").strip()
    ]
    seen = {str(q.get("text", "")).strip().lower() for q in kept}
    out = list(kept)
    for q in fresh or []:
        if not isinstance(q, dict):
            continue
        text = str(q.get("text", "")).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(q)
    return out


def _findings(steps: list[RunStep]) -> str:
    """The gathered material, as the text appended to the prep context.

    Only successful observations carry information, but a failed call is still worth
    naming — it tells the write-up (and the reader) that a lookup was attempted and
    came back empty, which is different from never having tried.
    """
    if not steps:
        return ""
    lines = []
    for step in steps:
        head = f"[{step.index}] {step.server}.{step.tool}"
        if step.why:
            head += f" — {step.why}"
        lines.append(head)
        got = step.observation if step.ok and step.observation else ""
        lines.append(got or f"  (no result: {step.detail})")
    return "\n".join(lines)


def findings_from_dicts(steps: list[dict[str, Any]] | None) -> str:
    """Render already-gathered material from persisted step dicts (see :func:`_findings`).

    The resume counterpart of :func:`_findings`: it reads the ``todo_delegations.steps``
    shape (dicts, not :class:`RunStep`) so a follow-up run can be handed everything the
    conversation has gathered *so far* and build on it, instead of re-running the same
    lookups from cold.
    """
    lines: list[str] = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        head = f"[{s.get('index')}] {s.get('server')}.{s.get('tool')}"
        if s.get("why"):
            head += f" — {s['why']}"
        lines.append(head)
        got = s.get("observation") if s.get("ok") and s.get("observation") else ""
        lines.append(got or f"  (no result: {s.get('detail')})")
    return "\n".join(lines)


def thread_context(messages: list[dict[str, Any]] | None) -> str:
    """Render the follow-up conversation as context for a resumed run, or ``""``.

    The free-form transcript the user builds up over a delegation (Phase 2): each
    ``{role, kind, text}`` turn becomes a labelled line so a re-run reads the
    back-and-forth as plain history and *continues* it rather than restarting cold.
    Blank turns are skipped.
    """
    lines: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        who = "You" if m.get("role") == "user" else "Assistant"
        lines.append(f"{who}: {text}")
    if not lines:
        return ""
    return "The follow-up conversation so far:\n" + "\n".join(lines)


def run_research(
    title: str,
    notes: str | None = None,
    *,
    context: str | None = None,
    client: Generator | None = None,
    toolbox: Toolbox | None = None,
    budget: RunBudget | None = None,
    now: datetime | None = None,
    already_asked: bool = False,
) -> RunResult:
    """Gather material for ``title`` with a bounded loop of allowlisted tool calls.

    Returns what it learned, anything it needs to ask the user, and why it stopped;
    the caller folds ``findings`` into the prep context so the write-up is produced by
    the existing, well-tested :func:`prefrontal.delegation.generate_prep` rather than a
    second generator.

    Never raises. With no client, no toolbox, or an empty toolbox it returns
    immediately with an honest ``stop_reason`` — auto mode then behaves exactly like
    today's ``agent`` prep.

    Args:
        title: The task text.
        notes: Any notes already on the todo.
        context: Free-text context supplied at delegation time.
        client: An Ollama-/Anthropic-like generator to drive the loop.
        toolbox: The unattended tools (see :func:`build_toolbox`).
        budget: Step/time/size limits; the defaults if omitted.
        now: Injectable clock for the deadline (tests).
        already_asked: True on a re-run after the user answered — presses the model to
            conclude instead of asking a fresh question every round.
    """
    budget = budget or RunBudget()
    started = now or utcnow()
    if toolbox is None or not toolbox.tools or toolbox.call is None:
        return RunResult(
            stop_reason="no_tools", detail="no tools are enabled for unattended use"
        )
    if client is None:
        return RunResult(
            stop_reason="model_unavailable", detail="no model available to plan the work"
        )

    task = f"Task: {title}"
    if notes:
        task += f"\nNotes: {notes}"
    if context:
        task += f"\nContext provided by the user:\n{context}"
    catalog = _catalog(toolbox)
    available = toolbox.names()

    system = _LOOP_SYSTEM + (_FINISH_UP if already_asked else "")

    steps: list[RunStep] = []
    questions: list[RunQuestion] = []
    transcript: list[str] = []
    seen_calls: dict[str, int] = {}  # canonical call -> the step that made it
    stop_reason = "budget"
    while len(steps) < budget.max_steps:
        if (utcnow() - started).total_seconds() >= budget.deadline_seconds:
            stop_reason = "deadline"
            break
        move = _next_turn(
            client, task, catalog, transcript, budget.max_steps - len(steps), system=system
        )
        if move is None:
            stop_reason = "stalled"
            break
        action = str(move.get("action", "")).strip().lower()
        if action == "ask":
            questions = _coerce_questions(move.get("questions"))
            if questions:
                stop_reason = "needs_input"
                break
            # An "ask" with nothing askable in it is just a malformed "done" — take
            # the write-up we can get rather than parking the todo on an empty form.
            stop_reason = "done"
            break
        if action != "call":
            # "done" — or anything that isn't a call, which we read as done rather
            # than arguing with the model about its JSON.
            stop_reason = "done"
            break
        name = str(move.get("tool", "")).strip()
        why = str(move.get("why", "")).strip()[:200]
        args = move.get("arguments")
        args = args if isinstance(args, dict) else {}
        key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if name not in available:
            # A hallucinated tool. Record it as a failed step (so the trail shows
            # what happened) and let the next turn see the correction.
            step = RunStep(
                index=len(steps) + 1, server="", tool=name, arguments=args, ok=False,
                detail=f'no such tool — pick one of: {", ".join(sorted(available))}',
                why=why,
            )
        elif key in seen_calls:
            # An identical call it already made. Observed for real: a local model will
            # re-run "search HELOC rates" four ways and burn the whole budget on one
            # fact. Refuse deterministically rather than trusting the don't-repeat
            # instruction — the step is still recorded, so the budget stays finite and
            # the next turn sees the correction.
            step = RunStep(
                index=len(steps) + 1, server=name.split(".")[0], tool=name.partition(".")[2],
                arguments=args, ok=False,
                detail=f"already called at step {seen_calls[key]} — use that result "
                       "or try something different",
                why=why,
            )
        else:
            seen_calls[key] = len(steps) + 1
            server, _, tool = name.partition(".")
            ok, content, detail = toolbox.call(server, tool, args)
            step = RunStep(
                index=len(steps) + 1, server=server, tool=tool, arguments=args, ok=ok,
                detail=detail, observation=_truncate(content, budget.observation_chars),
                why=why,
            )
        steps.append(step)
        got = step.observation if step.ok and step.observation else ""
        transcript.append(
            f"Step {step.index}: called {name} with {json.dumps(step.arguments, default=str)}\n"
            f"Result: {got or f'FAILED — {step.detail}'}"
        )

    detail = f"{len(steps)} tool call{'s' if len(steps) != 1 else ''}"
    detail += {
        "done": " (finished)",
        "needs_input": f" ({len(questions)} question{'s' if len(questions) != 1 else ''} for you)",
        "budget": " (hit the step limit)",
        "deadline": " (hit the time limit)",
        "stalled": " (the model stopped responding usefully)",
    }.get(stop_reason, "")
    return RunResult(
        steps=steps,
        findings=_findings(steps),
        questions=questions,
        stop_reason=stop_reason,
        detail=detail,
    )
