"""Tests for auto mode — bounded agentic runs on a delegated todo (roadmap M4).

Three layers: the pure loop (:mod:`prefrontal.autorun` — budgets, the unattended
gate, the ``ask`` round-trip) against a scripted model and a fake MCP server; the
``auto`` delegation handler; and the HTTP surface (``POST /todos/{id}/delegate``
with ``handler="auto"`` and ``POST /todos/{id}/delegate/answers``).

The safety properties under test are the ones the design leans on: a tool runs only
if it is *both* allowlisted and declared unattended, the loop cannot exceed its step
budget, a hallucinated tool never reaches a server, and a run that can't finish parks
with its partial work rather than guessing.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from prefrontal.autorun import (
    MAX_QUESTIONS,
    RunBudget,
    Toolbox,
    answered_context,
    build_toolbox,
    findings_from_dicts,
    merge_questions,
    run_research,
    thread_context,
)
from prefrontal.config import McpServerConfig, Settings, _parse_mcp_servers
from prefrontal.delegation import (
    HANDLER_AUTO,
    STATUS_NEEDS_INPUT,
    STATUS_PREPPED,
    DelegationResult,
    delegation_notice,
    run_delegation,
)
from prefrontal.integrations.mcp import McpClient
from prefrontal.integrations.ollama import OllamaClient
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from tests.conftest import scoped_default

SECRET = "autorun-secret"


# -- fakes -------------------------------------------------------------------


def _scripted(*replies: str) -> OllamaClient:
    """An OllamaClient that returns ``replies`` in order (then empty objects)."""
    seq = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        text = seq.pop(0) if seq else "{}"
        return httpx.Response(200, json={"response": text})

    return OllamaClient(transport=httpx.MockTransport(handler))


def _call(tool: str, why: str = "looking it up", **arguments) -> str:
    return json.dumps({"action": "call", "tool": tool, "arguments": arguments, "why": why})


_DONE = json.dumps({"action": "done", "why": "got what I need"})


def _toolbox(*, tools=("research.search",), result=(True, "HELOC rates average 8.1%", "ok")):
    """A Toolbox over fake tools, recording every call it receives."""
    calls: list[tuple[str, str, dict]] = []

    def call(server: str, tool: str, arguments: dict) -> tuple[bool, str, str]:
        calls.append((server, tool, arguments))
        return result

    box = Toolbox(
        tools=[
            {"server": t.split(".")[0], "tool": t.split(".")[1], "description": f"{t} desc",
             "input_schema": {"properties": {"query": {"type": "string"}}}}
            for t in tools
        ],
        call=call,
    )
    return box, calls


def _mcp_transport(tools, *, text="fake result", is_error=False):
    """A MockTransport JSON-RPC MCP server advertising ``tools``."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        rid, method = body["id"], body.get("method")

        def ok(result):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "result": result})

        if method == "initialize":
            return ok({"capabilities": {}})
        if method == "tools/list":
            return ok({"tools": tools})
        if method == "tools/call":
            return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": rid, "error": {"message": "x"}})

    return httpx.MockTransport(handler)


RESEARCH_TOOLS = [
    {"name": "search", "description": "Search the web", "inputSchema": {}},
    {"name": "publish", "description": "Publish a page", "inputSchema": {}},
]


def _mcp_factory(tools=RESEARCH_TOOLS, **kw):
    return lambda server: McpClient(server.url, transport=_mcp_transport(tools, **kw))


def _settings(*, allowed=("search", "publish"), unattended=("search",)):
    return Settings(
        webhook_secret=SECRET,
        mcp_servers=(
            McpServerConfig(
                name="research",
                url="https://mcp.test/rpc",
                allowed_tools=frozenset(allowed),
                unattended_tools=frozenset(unattended),
            ),
        ),
    )


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


# -- the loop: honest no-ops -------------------------------------------------


def test_no_toolbox_is_an_honest_no_op():
    """No unattended tools → no calls, and a reason that says so."""
    run = run_research("Should I get a HELOC?", client=_scripted(_call("research.search")))
    assert run.stop_reason == "no_tools"
    assert run.calls == 0 and run.findings == ""


def test_no_model_is_an_honest_no_op():
    box, calls = _toolbox()
    run = run_research("Should I get a HELOC?", client=None, toolbox=box)
    assert run.stop_reason == "model_unavailable"
    assert calls == []


def test_empty_toolbox_counts_as_no_tools():
    box, _calls = _toolbox(tools=())
    run = run_research("x", client=_scripted(_DONE), toolbox=box)
    assert run.stop_reason == "no_tools"


# -- the loop: happy path ----------------------------------------------------


def test_calls_a_tool_then_finishes():
    box, calls = _toolbox()
    run = run_research(
        "Should I get a HELOC for the kitchen?",
        notes="~40k budget",
        client=_scripted(_call("research.search", query="heloc rates"), _DONE),
        toolbox=box,
    )
    assert run.stop_reason == "done"
    assert calls == [("research", "search", {"query": "heloc rates"})]
    assert run.calls == 1
    step = run.steps[0]
    assert (step.index, step.server, step.tool, step.ok) == (1, "research", "search", True)
    # The findings carry the observation onward to the write-up.
    assert "8.1%" in run.findings
    assert "research.search" in run.findings


def test_multi_step_run_accumulates_findings():
    box, calls = _toolbox(tools=("research.search", "research.fetch"))
    run = run_research(
        "Compare HELOC vs cash-out refi",
        client=_scripted(
            _call("research.search", query="heloc"),
            _call("research.fetch", url="http://x"),
            _DONE,
        ),
        toolbox=box,
    )
    assert [c[1] for c in calls] == ["search", "fetch"]
    assert run.stop_reason == "done" and run.calls == 2
    assert [s.index for s in run.steps] == [1, 2]


def test_prior_steps_are_visible_to_the_next_turn():
    """The transcript is fed back, so the model can build on what it learned."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["prompt"])
        return httpx.Response(200, json={"response": _DONE if len(seen) > 1 else _call(
            "research.search", query="q")})

    box, _calls = _toolbox()
    run_research("t", client=OllamaClient(transport=httpx.MockTransport(handler)), toolbox=box)
    assert "Work so far:" not in seen[0]
    assert "Step 1: called research.search" in seen[1]
    assert "8.1%" in seen[1]


# -- the loop: budgets are hard ---------------------------------------------


def test_step_budget_is_a_hard_cap():
    """A model that never stops is stopped — and the reason is recorded, not silent."""
    box, calls = _toolbox()
    run = run_research(
        "endless",
        # Distinct arguments each turn, so it's the budget stopping this and not the
        # repeat-call guard.
        client=_scripted(*[_call("research.search", query=f"q{i}") for i in range(20)]),
        toolbox=box,
        budget=RunBudget(max_steps=3),
    )
    assert len(calls) == 3
    assert run.stop_reason == "budget"
    assert "step limit" in run.detail


def test_deadline_stops_the_run_before_the_next_turn():
    box, calls = _toolbox()
    run = run_research(
        "slow",
        client=_scripted(*[_call("research.search")] * 5),
        toolbox=box,
        budget=RunBudget(deadline_seconds=0.0),
    )
    assert calls == [] and run.stop_reason == "deadline"


def test_observation_is_truncated_and_says_so():
    box, _calls = _toolbox(result=(True, "x" * 5000, "ok"))
    run = run_research(
        "big",
        client=_scripted(_call("research.search"), _DONE),
        toolbox=box,
        budget=RunBudget(observation_chars=100),
    )
    obs = run.steps[0].observation
    assert len(obs) < 300
    assert "truncated" in obs and "4900 more characters" in obs


# -- the loop: the tool gate ------------------------------------------------


def test_hallucinated_tool_never_reaches_a_server():
    box, calls = _toolbox()
    run = run_research(
        "t", client=_scripted(_call("research.nope"), _DONE), toolbox=box
    )
    assert calls == []  # nothing was dialed
    assert run.calls == 1  # but the attempt is on the record
    assert run.steps[0].ok is False
    assert "no such tool" in run.steps[0].detail
    assert "research.search" in run.steps[0].detail  # the correction names the options


def test_failed_tool_is_recorded_not_raised():
    box, _calls = _toolbox(result=(False, "", "server exploded"))
    run = run_research("t", client=_scripted(_call("research.search"), _DONE), toolbox=box)
    assert run.steps[0].ok is False
    assert "no result: server exploded" in run.findings


def test_identical_repeat_call_is_refused_deterministically():
    """Observed against the local model: it re-runs the same search until the budget
    is gone. The don't-repeat *instruction* isn't trusted — the loop enforces it."""
    box, calls = _toolbox()
    run = run_research(
        "t",
        client=_scripted(*[_call("research.search", query="heloc rates")] * 4, _DONE),
        toolbox=box,
    )
    assert len(calls) == 1  # only the first one reached the tool
    assert run.calls == 4  # the repeats are on the record, and cost budget
    assert run.steps[1].ok is False
    assert "already called at step 1" in run.steps[1].detail
    # A *different* argument isn't a repeat.
    box2, calls2 = _toolbox()
    run_research(
        "t",
        client=_scripted(
            _call("research.search", query="a"), _call("research.search", query="b"), _DONE
        ),
        toolbox=box2,
    )
    assert len(calls2) == 2


def test_already_asked_presses_the_model_to_conclude():
    """A re-run after answers must not ping-pong a fresh question every round."""
    systems: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        systems.append(json.loads(request.content).get("system") or "")
        return httpx.Response(200, json={"response": _DONE})

    box, _calls = _toolbox()
    client = OllamaClient(transport=httpx.MockTransport(handler))
    run_research("t", client=client, toolbox=box)
    assert "ALREADY asked" not in systems[0]
    run_research("t", client=client, toolbox=box, already_asked=True)
    assert "ALREADY asked" in systems[1]


def test_questions_are_asked_in_the_second_person():
    """The prompt has to say so: the local model otherwise writes them as the user
    ("What is my credit score?"), which reads as nonsense on the card."""
    from prefrontal.autorun import _LOOP_SYSTEM

    assert "second person" in _LOOP_SYSTEM


def test_unusable_model_reply_stalls_gracefully():
    box, calls = _toolbox()
    run = run_research("t", client=_scripted("I'm afraid I can't do that, Dave"), toolbox=box)
    assert calls == [] and run.stop_reason == "stalled"
    assert "stopped responding" in run.detail


# -- the loop: asking ------------------------------------------------------


def _ask(*questions) -> str:
    return json.dumps({"action": "ask", "questions": list(questions)})


def test_ask_parks_with_questions():
    """The HELOC case: it researches what it can, then asks what only you know."""
    box, calls = _toolbox()
    run = run_research(
        "Should I get a HELOC for the kitchen remodel?",
        client=_scripted(
            _call("research.search", query="heloc rates"),
            _ask(
                {"text": "What's your current mortgage rate?", "why": "decides if a refi wins"},
                {"text": "Roughly how much equity do you have?", "why": "sets the ceiling"},
            ),
        ),
        toolbox=box,
    )
    assert run.stop_reason == "needs_input"
    assert len(calls) == 1  # it did the lookup it could do first
    assert [q.text for q in run.questions] == [
        "What's your current mortgage rate?",
        "Roughly how much equity do you have?",
    ]
    assert run.questions[0].why == "decides if a refi wins"
    assert run.questions[0].answer is None
    assert "2 questions for you" in run.detail
    # The findings still made it out — a parked run isn't a lost run.
    assert "8.1%" in run.findings


def test_questions_are_capped_deduped_and_accept_bare_strings():
    box, _calls = _toolbox()
    run = run_research(
        "t",
        client=_scripted(_ask("Rate?", "Rate?", *[f"Q{i}?" for i in range(10)])),
        toolbox=box,
    )
    texts = [q.text for q in run.questions]
    assert len(texts) == MAX_QUESTIONS
    assert texts[0] == "Rate?" and texts.count("Rate?") == 1


def test_ask_with_nothing_askable_is_treated_as_done():
    box, _calls = _toolbox()
    run = run_research("t", client=_scripted(_ask()), toolbox=box)
    assert run.stop_reason == "done" and run.questions == []


# -- the answer round-trip (pure helpers) ----------------------------------


def test_answered_context_renders_only_answered_pairs():
    text = answered_context([
        {"text": "Rate?", "answer": "6.2%"},
        {"text": "Equity?", "answer": None},
        {"text": "Timeline?", "answer": "   "},
    ])
    assert "Q: Rate?" in text and "A: 6.2%" in text
    assert "Equity?" not in text  # unanswered questions must not invite invention
    assert answered_context([]) == "" and answered_context(None) == ""


def test_merge_questions_keeps_answers_and_drops_stale_asks():
    prior = [
        {"text": "Rate?", "answer": "6.2%"},
        {"text": "Equity?", "answer": None},  # never answered → superseded
    ]
    fresh = [{"text": "Rate?", "answer": None}, {"text": "Contractor quote?", "answer": None}]
    merged = merge_questions(prior, fresh)
    assert [q["text"] for q in merged] == ["Rate?", "Contractor quote?"]
    # The answered one keeps its answer and isn't re-asked.
    assert merged[0]["answer"] == "6.2%"


# -- build_toolbox: the unattended gate -----------------------------------


def test_build_toolbox_narrows_to_unattended_tools(store):
    box = build_toolbox(store, _settings(), client_factory=_mcp_factory())
    # `publish` is allowlisted and advertised, but not declared unattended.
    assert [t["tool"] for t in box.tools] == ["search"]
    assert box.names() == {"research.search"}


def test_build_toolbox_empty_when_nothing_is_declared_unattended(store):
    box = build_toolbox(store, _settings(unattended=()), client_factory=_mcp_factory())
    assert box.tools == [] and box.call is None


def test_build_toolbox_refuses_an_undeclared_tool_even_if_named(store):
    """Belt and braces: the loop is told what exists, but the gate is in the call."""
    box = build_toolbox(store, _settings(), client_factory=_mcp_factory())
    ok, content, detail = box.call("research", "publish", {})
    assert ok is False and content == ""
    assert "not allowed to run unattended" in detail


def test_build_toolbox_call_reaches_the_server_and_audits(store):
    box = build_toolbox(store, _settings(), client_factory=_mcp_factory(text="rates are 8%"))
    ok, content, _detail = box.call("research", "search", {"query": "x"})
    assert ok is True and content == "rates are 8%"
    episodes = store.recent_episodes(limit=10)
    assert any(e["episode_type"] == "action" for e in episodes)


def test_config_intersects_unattended_with_allowed():
    """`unattended_tools` can never widen the confirm-gated allowlist."""
    servers = _parse_mcp_servers(json.dumps([{
        "name": "research",
        "url": "https://x/rpc",
        "allowed_tools": ["search"],
        "unattended_tools": ["search", "publish"],  # publish isn't even allowlisted
    }]))
    assert servers[0].unattended_tools == frozenset({"search"})


def test_config_unattended_defaults_to_empty():
    servers = _parse_mcp_servers(json.dumps([
        {"name": "r", "url": "https://x/rpc", "allowed_tools": ["search"]}
    ]))
    assert servers[0].unattended_tools == frozenset()


# -- the delegation handler ------------------------------------------------


def _prep(brief="Here's the analysis.") -> str:
    return json.dumps({"brief": brief, "drafts": [], "actions": []})


def test_auto_handler_researches_then_preps(store):
    tid = store.add_todo("Should I get a HELOC?")
    box, calls = _toolbox()
    result = run_delegation(
        store,
        store.get_todo(tid),
        handler=HANDLER_AUTO,
        client=_scripted(_call("research.search"), _DONE, _prep("HELOC beats a refi here.")),
        toolbox=box,
    )
    assert result.status == STATUS_PREPPED
    assert result.brief == "HELOC beats a refi here."
    assert "researched the task" in result.detail
    assert len(calls) == 1
    # The trail is persisted on the row, not just returned.
    stored = store.get_delegation(tid)
    assert stored["handler"] == HANDLER_AUTO
    assert stored["steps"][0]["tool"] == "search"
    assert stored["steps"][0]["ok"] is True


def test_auto_handler_feeds_findings_into_the_prep(store):
    """The write-up sees what the run gathered (as context, like pasted material)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["prompt"]
        seen.append(prompt)
        reply = _call("research.search") if len(seen) == 1 else (
            _DONE if len(seen) == 2 else _prep()
        )
        return httpx.Response(200, json={"response": reply})

    tid = store.add_todo("HELOC?")
    box, _calls = _toolbox()
    run_delegation(
        store, store.get_todo(tid), handler=HANDLER_AUTO,
        client=OllamaClient(transport=httpx.MockTransport(handler)), toolbox=box,
    )
    assert "8.1%" in seen[-1]  # the prep prompt carries the gathered material
    assert "gathered" in seen[-1]


def test_auto_handler_with_no_tools_degrades_to_plain_prep(store):
    tid = store.add_todo("Book dentist")
    result = run_delegation(
        store, store.get_todo(tid), handler=HANDLER_AUTO, client=_scripted(_prep("Call them.")),
    )
    assert result.status == STATUS_PREPPED
    assert result.brief == "Call them."
    assert result.steps == []
    assert "no tools used" in result.detail


def test_auto_handler_parks_on_questions_with_partial_output(store):
    tid = store.add_todo("Should I get a HELOC?")
    box, _calls = _toolbox()
    result = run_delegation(
        store, store.get_todo(tid), handler=HANDLER_AUTO,
        client=_scripted(
            _ask({"text": "Your rate?", "why": "sets the comparison"}),
            _prep("Partial: rates are ~8%."),
        ),
        toolbox=box,
    )
    assert result.status == STATUS_NEEDS_INPUT
    assert result.brief == "Partial: rates are ~8%."  # never a bare "waiting"
    assert [q["text"] for q in result.questions] == ["Your rate?"]
    stored = store.get_delegation(tid)
    assert stored["status"] == STATUS_NEEDS_INPUT
    assert stored["questions"][0]["answer"] is None


def test_answers_are_fed_back_and_not_re_asked(store):
    """A re-run gets the Q&A as facts, and the answered question survives the write."""
    tid = store.add_todo("Should I get a HELOC?")
    box, _calls = _toolbox()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["prompt"])
        return httpx.Response(200, json={"response": _DONE if len(seen) == 1 else _prep()})

    result = run_delegation(
        store, store.get_todo(tid), handler=HANDLER_AUTO,
        client=OllamaClient(transport=httpx.MockTransport(handler)), toolbox=box,
        answered=[{"text": "Your rate?", "why": "", "answer": "6.2%"}],
    )
    assert "6.2%" in seen[0]  # the loop starts with what it was told
    assert result.status == STATUS_PREPPED
    stored = store.get_delegation(tid)
    assert stored["questions"] == [{"text": "Your rate?", "why": "", "answer": "6.2%"}]


def test_broken_json_prep_reply_lifts_out_the_brief(store):
    """Seen live: a prep reply that's JSON-shaped but unparseable (an unterminated
    string) used to be stored verbatim, putting a wall of JSON on the todo card."""
    tid = store.add_todo("HELOC?")
    broken = (
        '```\n{\n  "brief": "Rates are ~8.1%.\nA refi may beat it.",\n'
        '  "drafts": [{"channel": "email", "body": "Hi "unquoted" oops\n'
    )
    result = run_delegation(
        store, store.get_todo(tid), handler=HANDLER_AUTO, client=_scripted(broken),
    )
    assert result.brief == "Rates are ~8.1%.\nA refi may beat it."
    assert "{" not in result.brief and '"drafts"' not in result.brief


def test_prose_prep_reply_is_still_kept_verbatim(store):
    """The salvage path's original job — a useful non-JSON answer — is unchanged."""
    tid = store.add_todo("HELOC?")
    result = run_delegation(
        store, store.get_todo(tid), handler=HANDLER_AUTO,
        client=_scripted("A HELOC makes sense if you stay under 80% CLTV."),
    )
    assert result.brief == "A HELOC makes sense if you stay under 80% CLTV."


def test_notice_leads_with_progress_and_names_the_first_question():
    msg = delegation_notice("HELOC report", DelegationResult(
        handler=HANDLER_AUTO, status=STATUS_NEEDS_INPUT, brief="partial",
        questions=[{"text": "Your rate?", "answer": None},
                   {"text": "Equity?", "answer": None}],
    ))
    assert "Got a start" in msg and "2 things" in msg
    assert "Your rate?" in msg


# -- the store ------------------------------------------------------------


def test_answer_delegation_questions_is_positional_and_sparse(store):
    tid = store.add_todo("t")
    store.set_delegation(
        tid, handler=HANDLER_AUTO, status=STATUS_NEEDS_INPUT,
        questions=[{"text": "A?", "answer": None}, {"text": "B?", "answer": None},
                   {"text": "C?", "answer": None}],
    )
    # Answer the second only (blank skips), and don't send the third at all.
    updated = store.answer_delegation_questions(tid, ["", "yes"])
    assert [q["answer"] for q in updated] == [None, "yes", None]
    # Later answers merge in rather than replacing the list.
    updated = store.answer_delegation_questions(tid, ["first", None, "third"])
    assert [q["answer"] for q in updated] == ["first", "yes", "third"]
    assert store.get_delegation(tid)["questions"][2]["answer"] == "third"


def test_answer_delegation_questions_missing_row(store):
    tid = store.add_todo("t")
    assert store.answer_delegation_questions(tid, ["x"]) is None


def test_append_delegation_message_thread(store):
    tid = store.add_todo("t")
    store.set_delegation(tid, handler=HANDLER_AUTO, status=STATUS_PREPPED, brief="v1")
    msgs = store.append_delegation_message(tid, role="user", text="focus on the 15-year option")
    assert msgs == [{"role": "user", "kind": "message", "text": "focus on the 15-year option"}]
    msgs = store.append_delegation_message(tid, role="agent", text="On it.", kind="note")
    assert [m["role"] for m in msgs] == ["user", "agent"]
    # A blank turn is a no-op that still returns the current thread.
    assert store.append_delegation_message(tid, role="user", text="   ") == msgs
    assert store.get_delegation(tid)["messages"] == msgs


def test_append_delegation_message_missing_row(store):
    tid = store.add_todo("t")
    assert store.append_delegation_message(tid, role="user", text="hi") is None


def test_set_delegation_round_trips_messages(store):
    tid = store.add_todo("t")
    thread = [{"role": "user", "kind": "message", "text": "hi"}]
    store.set_delegation(tid, handler=HANDLER_AUTO, status=STATUS_PREPPED, messages=thread)
    assert store.get_delegation(tid)["messages"] == thread


# -- resume rendering (pure) ----------------------------------------------


def test_thread_context_renders_the_conversation():
    assert thread_context([]) == ""
    ctx = thread_context([
        {"role": "user", "text": "focus on 15-year"},
        {"role": "agent", "text": "On it."},
        {"role": "user", "text": "   "},  # blank turn skipped
    ])
    assert "You: focus on 15-year" in ctx
    assert "Assistant: On it." in ctx
    assert ctx.count("\n") == 2  # header + two non-blank turns


def test_findings_from_dicts_renders_persisted_steps():
    assert findings_from_dicts([]) == ""
    f = findings_from_dicts([
        {"index": 1, "server": "research", "tool": "search", "ok": True,
         "observation": "rate is 8.1%", "why": "current rate"},
        {"index": 2, "server": "research", "tool": "fetch", "ok": False, "detail": "timeout"},
    ])
    assert "research.search — current rate" in f
    assert "rate is 8.1%" in f
    assert "(no result: timeout)" in f


# -- HTTP surface ---------------------------------------------------------


@pytest.fixture()
def http(monkeypatch):
    """A TestClient whose model replies are scripted per-request."""
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", "")
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "me", display_name="Me", token=SECRET, is_operator=True)
    from prefrontal.webhooks.app import create_app

    replies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        text = replies.pop(0) if replies else _prep()
        return httpx.Response(200, json={"response": text})

    ollama = OllamaClient(transport=httpx.MockTransport(handler))
    app = create_app(store=unscoped, settings=_settings(), ollama=ollama)
    with TestClient(app) as c:
        yield c, replies
    conn.close()


def _headers():
    return {"X-Prefrontal-Token": SECRET}


@pytest.fixture()
def fake_mcp(monkeypatch):
    """Point the real action layer's client factory at a fake MCP server."""
    monkeypatch.setattr(
        "prefrontal.actions._default_factory",
        lambda server: McpClient(server.url, transport=_mcp_transport(RESEARCH_TOOLS, text="8.1%")),
    )


def test_http_delegate_auto_reports_its_trail(http, fake_mcp):
    client, replies = http
    # Create first: POST /todos runs its own (augmentation) model call, which would
    # otherwise eat the first scripted reply.
    tid = client.post(
        "/todos", json={"title": "HELOC report"}, headers=_headers()
    ).json()["todo_id"]
    replies.extend([_call("research.search"), _DONE, _prep("Go with the HELOC.")])
    body = client.post(
        f"/todos/{tid}/delegate", json={"handler": "auto"}, headers=_headers()
    ).json()
    assert body["status"] == STATUS_PREPPED
    assert body["brief"] == "Go with the HELOC."
    assert [s["tool"] for s in body["steps"]] == ["search"]
    assert body["questions"] == []


def test_http_answers_records_and_reruns(http, fake_mcp):
    client, replies = http
    tid = client.post(
        "/todos", json={"title": "HELOC report"}, headers=_headers()
    ).json()["todo_id"]
    replies.extend([_ask({"text": "Your rate?", "why": "w"}), _prep("Partial.")])
    first = client.post(
        f"/todos/{tid}/delegate", json={"handler": "auto", "context": "kitchen ~40k"},
        headers=_headers(),
    ).json()
    assert first["status"] == STATUS_NEEDS_INPUT
    assert [q["text"] for q in first["questions"]] == ["Your rate?"]

    replies.extend([_DONE, _prep("Final: take the HELOC.")])
    second = client.post(
        f"/todos/{tid}/delegate/answers", json={"answers": ["6.2%"]}, headers=_headers()
    ).json()
    assert second["status"] == STATUS_PREPPED
    assert second["brief"] == "Final: take the HELOC."
    stored = client.get("/todos", headers=_headers()).json()["todos"][0]["delegation"]
    assert stored["questions"][0]["answer"] == "6.2%"
    # The original context survives the re-run (the findings don't need to).
    assert stored["context"] == "kitchen ~40k"


def test_http_answers_rejects_a_non_auto_delegation(http):
    client, replies = http
    replies.append(_prep())
    tid = client.post("/todos", json={"title": "t"}, headers=_headers()).json()["todo_id"]
    client.post(f"/todos/{tid}/delegate", json={"handler": "agent"}, headers=_headers())
    r = client.post(f"/todos/{tid}/delegate/answers", json={"answers": ["x"]}, headers=_headers())
    assert r.status_code == 422
    assert "auto" in r.json()["detail"]


def test_http_message_appends_thread_and_resumes(http, fake_mcp):
    """A free-form follow-up records the exchange and continues the research: the
    trail accumulates (prior + this run's lookups) rather than restarting."""
    client, replies = http
    tid = client.post(
        "/todos", json={"title": "HELOC report"}, headers=_headers()
    ).json()["todo_id"]
    replies.extend([_call("research.search"), _DONE, _prep("v1")])
    first = client.post(
        f"/todos/{tid}/delegate", json={"handler": "auto"}, headers=_headers()
    ).json()
    assert first["status"] == STATUS_PREPPED
    assert len(first["steps"]) == 1

    replies.extend([_call("research.search"), _DONE, _prep("v2: the 15-year path")])
    second = client.post(
        f"/todos/{tid}/delegate/message",
        json={"message": "focus on the 15-year option"}, headers=_headers(),
    ).json()
    assert second["status"] == STATUS_PREPPED
    assert second["brief"] == "v2: the 15-year path"
    # The trail accumulated across the conversation (prior search + this run's),
    # renumbered to a single contiguous sequence (each run indexes from 1).
    assert len(second["steps"]) == 2
    assert [s["index"] for s in second["steps"]] == [1, 2]
    # The thread records the user's message and the agent's reply, in order.
    assert [m["role"] for m in second["messages"]] == ["user", "agent"]
    assert second["messages"][0]["text"] == "focus on the 15-year option"
    stored = client.get("/todos", headers=_headers()).json()["todos"][0]["delegation"]
    assert [m["role"] for m in stored["messages"]] == ["user", "agent"]


def test_http_message_rejects_a_non_auto_delegation(http):
    client, replies = http
    replies.append(_prep())
    tid = client.post("/todos", json={"title": "t"}, headers=_headers()).json()["todo_id"]
    client.post(f"/todos/{tid}/delegate", json={"handler": "agent"}, headers=_headers())
    r = client.post(f"/todos/{tid}/delegate/message", json={"message": "hi"}, headers=_headers())
    assert r.status_code == 422


def test_http_message_blank_is_422(http, fake_mcp):
    client, replies = http
    tid = client.post("/todos", json={"title": "t"}, headers=_headers()).json()["todo_id"]
    replies.extend([_DONE, _prep()])
    client.post(f"/todos/{tid}/delegate", json={"handler": "auto"}, headers=_headers())
    r = client.post(f"/todos/{tid}/delegate/message", json={"message": "   "}, headers=_headers())
    assert r.status_code == 422


def test_http_message_404s_without_a_delegation(http):
    client, _ = http
    tid = client.post("/todos", json={"title": "t"}, headers=_headers()).json()["todo_id"]
    r = client.post(f"/todos/{tid}/delegate/message", json={"message": "hi"}, headers=_headers())
    assert r.status_code == 404


def test_delegation_resolves_the_summarizer_agent(monkeypatch):
    """Delegation must honour ANTHROPIC_AGENTS like every other selectable agent.

    It didn't: the router read `services.summarizer` (the raw local client) directly,
    so the *same* hand-off ran on Claude from the NL box — which resolves the
    `assistant` agent — but on the local model from the dashboard button and the CLI.
    """
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", "")
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "me", display_name="Me", token=SECRET, is_operator=True)
    from prefrontal.webhooks.app import create_app

    # A stand-in "cloud" client that the resolver should pick for `summarizer`.
    class _Cloud:
        model = "claude-test"

        def available(self):
            return True

        def generate(self, prompt, *, system=None, num_ctx=None, timeout=None):
            return _prep("Written by the cloud model.")

    settings = Settings(webhook_secret=SECRET, anthropic_agents=("summarizer",))
    ollama = _scripted(_prep("Written by the local model."))
    app = create_app(store=unscoped, settings=settings, ollama=ollama, anthropic=_Cloud())
    with TestClient(app) as c:
        tid = c.post("/todos", json={"title": "t"}, headers=_headers()).json()["todo_id"]
        body = c.post(
            f"/todos/{tid}/delegate", json={"handler": "agent"}, headers=_headers()
        ).json()
    assert body["brief"] == "Written by the cloud model."
    conn.close()


def test_delegation_stays_local_when_the_agent_is_not_opted_in(monkeypatch):
    """The default has to stay local-first: no opt-in, no cloud call."""
    monkeypatch.setenv("PREFRONTAL_SECRET_KEY", "")
    conn = init_db(":memory:")
    unscoped = MemoryStore(conn)
    provision_user(unscoped, "me", display_name="Me", token=SECRET, is_operator=True)
    from prefrontal.webhooks.app import create_app

    class _Cloud:
        model = "claude-test"

        def available(self):
            return True

        def generate(self, prompt, *, system=None, num_ctx=None, timeout=None):
            raise AssertionError("must not reach the cloud provider")

    settings = Settings(webhook_secret=SECRET, anthropic_agents=("assistant",))
    ollama = _scripted(_prep("aug"), _prep("Written by the local model."))
    app = create_app(store=unscoped, settings=settings, ollama=ollama, anthropic=_Cloud())
    with TestClient(app) as c:
        tid = c.post("/todos", json={"title": "t"}, headers=_headers()).json()["todo_id"]
        body = c.post(
            f"/todos/{tid}/delegate", json={"handler": "agent"}, headers=_headers()
        ).json()
    assert body["brief"] == "Written by the local model."
    conn.close()


def test_prep_degrades_when_the_resolved_provider_fails(store):
    """A cloud failure has to fall back to the heuristic like a local one does.

    `generate_prep` caught only OllamaError, so once delegation could actually resolve
    to Claude an AnthropicError would escape as "prep failed unexpectedly".
    """
    from prefrontal.integrations.anthropic import AnthropicError

    class _Broken:
        def generate(self, prompt, *, system=None, num_ctx=None, timeout=None):
            raise AnthropicError("overloaded")

    tid = store.add_todo("Book dentist", notes="the one on Pine St")
    result = run_delegation(
        store, store.get_todo(tid), handler=HANDLER_AUTO, client=_Broken(),
    )
    assert result.status == STATUS_PREPPED
    assert "Generated offline" in result.brief  # the honest heuristic, not a crash
    assert "the one on Pine St" in result.brief


def test_http_answers_404s_without_a_delegation(http):
    client, _replies = http
    tid = client.post("/todos", json={"title": "t"}, headers=_headers()).json()["todo_id"]
    r = client.post(f"/todos/{tid}/delegate/answers", json={"answers": ["x"]}, headers=_headers())
    assert r.status_code == 404
