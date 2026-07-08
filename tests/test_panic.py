"""Tests for panic mode.

Covers the structured triage (bucketing commitments / todos / mail into
late / soon / piling-up), the single first-step selection, the deterministic
rendering (including the all-clear and no-hard-clock paths), the LLM-with-fallback
pass, and the /panic endpoint.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from prefrontal.config import Settings
from prefrontal.impact import utcnow
from prefrontal.integrations.anthropic import AnthropicError
from prefrontal.integrations.ollama import OllamaError
from prefrontal.memory.store import MemoryStore
from prefrontal.panic import (
    build_panic,
    evaluate_panic_check,
    overwhelm_level,
    panic_alert_message,
    render_panic,
    summarize_panic,
)
from prefrontal.webhooks.app import create_app
from tests.conftest import scoped_default

SECRET = "panic-secret"


def _at(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _disable_quiet_hours(store) -> None:
    """Set a degenerate responsive window (start == end = always responsive), so
    the proactive panic check never *defers* on wall-clock time — these tests
    exercise the edge/cooldown/button logic, not the quiet-hours gate."""
    store.set_state("responsive_hours_start", "0")
    store.set_state("responsive_hours_end", "0")


@pytest.fixture()
def store():
    with MemoryStore.open(":memory:") as s:
        yield scoped_default(s)


@pytest.fixture()
def noon():
    """A fixed midday 'now' so windowing never depends on wall-clock time."""
    return utcnow().replace(hour=12, minute=0, second=0, microsecond=0)


def test_panic_surfaces_cascade_knock_on(store, noon):
    """Being late to leave for one commitment topples the ones after it."""
    # Standup in 5 min with a 10-min lead → you're already 5 min late to leave;
    # it runs 30 min, so its overrun eats into a back-to-back Review.
    store.upsert_commitment(
        title="Standup", start_at=_at(noon + timedelta(minutes=5)),
        end_at=_at(noon + timedelta(minutes=35)), lead_minutes=10, external_id="w:1",
    )
    store.upsert_commitment(
        title="Review", start_at=_at(noon + timedelta(minutes=40)),
        end_at=_at(noon + timedelta(minutes=70)), lead_minutes=10, external_id="w:2",
    )
    plan = build_panic(store, now=noon)
    titles = [c["title"] for c in plan.cascade]
    assert titles == ["Standup", "Review"]  # schedule order, both at risk
    review = next(c for c in plan.cascade if c["title"] == "Review")
    assert review["caused_by"] == "Standup"
    assert "Knock-on" in render_panic(plan)


def test_panic_cascade_is_travel_aware(store, noon):
    """Far-flung venues topple under real drive time even with tiny static leads."""
    # Two upcoming commitments with small (5-min) leads — reachable on paper — but
    # their coordinates are far from the last known location, so the real drive
    # blows the buffer and both fall (chained).
    store.set_location(0.0, 0.0)
    store.upsert_commitment(
        title="Client pitch", start_at=_at(noon + timedelta(minutes=20)),
        end_at=_at(noon + timedelta(minutes=50)), lead_minutes=5,
        dest_lat=0.0, dest_lon=0.4, external_id="w:1",
    )
    store.upsert_commitment(
        title="Site visit", start_at=_at(noon + timedelta(minutes=70)),
        end_at=_at(noon + timedelta(minutes=100)), lead_minutes=5,
        dest_lat=0.0, dest_lon=0.8, external_id="w:2",
    )
    plan = build_panic(store, now=noon)
    titles = [c["title"] for c in plan.cascade]
    assert titles == ["Client pitch", "Site visit"]
    assert "Knock-on" in render_panic(plan)


def test_panic_no_cascade_for_a_lone_late_item(store, noon):
    """A single late commitment with nothing after it shows no knock-on chain."""
    store.upsert_commitment(
        title="Solo", start_at=_at(noon + timedelta(minutes=5)),
        end_at=_at(noon + timedelta(minutes=35)), lead_minutes=10, external_id="w:solo",
    )
    plan = build_panic(store, now=noon)
    assert plan.cascade == []
    assert "Knock-on" not in render_panic(plan)


def test_late_commitment_becomes_a_fire_and_drives_the_first_step(store, noon):
    """A commitment whose safe-departure has passed is 'late' and leads the plan."""
    store.upsert_commitment(
        title="Dentist",
        start_at=_at(noon + timedelta(minutes=30)),
        lead_minutes=60,  # should have left at 11:30, it's noon
        external_id="personal:1",
        hardness="hard",
    )
    plan = build_panic(store, now=noon)
    assert [p.title for p in plan.late] == ["Leave for Dentist"]
    assert plan.late[0].source == "Personal"  # calendar label is title-cased
    assert plan.counts["pressing"] == 1
    # The one thing points at getting out the door.
    assert plan.first_step_for == "Leave for Dentist"
    assert "door" in plan.first_step.lower()


def test_started_hard_commitment_is_late_soft_ongoing_is_ignored(store, noon):
    """A hard meeting already underway is a fire; a soft block you're in is not."""
    store.upsert_commitment(
        title="Standup", start_at=_at(noon - timedelta(minutes=15)),
        end_at=_at(noon + timedelta(minutes=15)), external_id="work:1", hardness="hard",
    )
    store.upsert_commitment(
        title="Focus block", start_at=_at(noon - timedelta(minutes=15)),
        end_at=_at(noon + timedelta(minutes=15)), external_id="work:2", hardness="soft",
    )
    plan = build_panic(store, now=noon)
    titles = [p.title for p in plan.late]
    assert "Get to Standup" in titles
    assert all("Focus block" not in t for t in titles)
    assert "started 15 min ago" in plan.late[0].when


def test_soon_bucket_holds_upcoming_departures(store, noon):
    """A commitment inside the soon-window (but not yet late) lands in 'soon'."""
    store.upsert_commitment(
        title="Lunch with Sam", start_at=_at(noon + timedelta(minutes=50)),
        lead_minutes=10, external_id="personal:1",
    )
    plan = build_panic(store, now=noon)
    assert not plan.late
    assert [p.title for p in plan.soon] == ["Leave for Lunch with Sam"]
    assert plan.soon[0].when.startswith("leave in")


def test_ended_and_fyi_commitments_are_excluded(store, noon):
    """Finished events and FYI (someone-else) events never appear."""
    store.upsert_commitment(
        title="Over already", start_at=_at(noon - timedelta(hours=2)),
        end_at=_at(noon - timedelta(hours=1)), external_id="work:1", hardness="hard",
    )
    store.upsert_commitment(
        title="Kid's recital", start_at=_at(noon + timedelta(minutes=30)),
        lead_minutes=60, external_id="family:1", hardness="hard", kind="fyi",
    )
    plan = build_panic(store, now=noon)
    assert plan.late == [] and plan.soon == []
    assert plan.counts["pressing"] == 0


def test_todo_deadlines_bucket_and_urgent_flag(store, noon):
    """Overdue → late, due-today → soon, priority-3 no-deadline → soon."""
    yesterday = (noon - timedelta(days=2)).strftime("%Y-%m-%d")  # date-only
    today = noon.strftime("%Y-%m-%d")
    store.add_todo("File the taxes", deadline=yesterday, priority=2)
    store.add_todo("Submit expense report", deadline=today, priority=1)
    store.add_todo("Fix the leak", priority=3)  # urgent, no deadline

    plan = build_panic(store, now=noon)
    assert [p.title for p in plan.late] == ["File the taxes"]
    assert plan.late[0].when.endswith("overdue")
    soon_titles = {p.title for p in plan.soon}
    assert soon_titles == {"Submit expense report", "Fix the leak"}


def test_late_commitment_outranks_a_stale_overdue_todo(store, noon):
    """A commitment you're late to leave for leads over any overdue todo.

    Coarse-tier contract: the "start here" first step is the commitment you're
    late for, never a stale todo — even a long-overdue, top-priority one (whose
    score must stay in the [800, 1000) band, below the commitment tiers).
    """
    store.upsert_commitment(
        title="Dentist", start_at=_at(noon + timedelta(minutes=30)),
        lead_minutes=60,  # should have left 30 min ago
        external_id="personal:1", hardness="hard",
    )
    # Very overdue (10 days) and top priority — the old scoring would push this
    # above the late commitment and make it the first step.
    store.add_todo(
        "Sort the garage", deadline=(noon - timedelta(days=10)).strftime("%Y-%m-%d"),
        priority=3,
    )
    plan = build_panic(store, now=noon)
    assert plan.late[0].title == "Leave for Dentist"
    assert plan.first_step_for == "Leave for Dentist"
    assert plan.late[0].score > plan.late[1].score  # commitment strictly above todo


def test_date_only_deadline_today_is_not_overdue(store, noon):
    """A todo due 'today' (date-only) is soon, not late — end-of-day, not midnight."""
    plan = build_panic(store, now=noon)
    store.add_todo("Renew passport", deadline=noon.strftime("%Y-%m-%d"))
    plan = build_panic(store, now=noon)
    assert [p.title for p in plan.late] == []
    assert [p.title for p in plan.soon] == ["Renew passport"]


def test_mail_urgency_splits_across_soon_and_piling_up(store, noon):
    """Urgent action-mail is 'soon'; high is 'piling_up'; each tagged by inbox."""
    store.record_mail(
        account="work", message_id="m1", sender_name="Boss", subject="Contract???",
        needs_action=True, urgency="urgent",
    )
    store.record_mail(
        account="home", message_id="m2", sender_name="School", subject="Form due",
        needs_action=True, urgency="high",
    )
    plan = build_panic(store, now=noon)
    assert any(p.kind == "mail" and p.source == "work" for p in plan.soon)
    assert any(p.kind == "mail" and p.source == "home" for p in plan.piling_up)


def test_first_step_prefers_a_clocked_item_over_a_smoulder(store, noon):
    """The forced first step comes from late/soon, never from piling-up alone."""
    store.record_mail(
        account="home", message_id="m2", sender_name="School", subject="Form",
        needs_action=True, urgency="high",  # piling_up only
    )
    store.add_todo("Reply to landlord", deadline=noon.strftime("%Y-%m-%d"))  # soon
    plan = build_panic(store, now=noon)
    assert plan.first_step_for == "Reply to landlord"
    assert plan.first_step  # a concrete, non-empty action


def test_render_all_clear(store, noon):
    """An empty board reassures rather than inventing work."""
    text = render_panic(build_panic(store, now=noon))
    assert "Nothing is actually on fire" in text


def test_render_no_hard_clock(store, noon):
    """Only piling-up items → a calmer 'nothing has a hard clock' grounding."""
    store.record_mail(
        account="home", message_id="m2", sender_name="School", subject="Form",
        needs_action=True, urgency="high",
    )
    text = render_panic(build_panic(store, now=noon))
    assert "nothing has a hard clock" in text.lower()
    assert "Piling up" in text


def test_render_full_triage_has_sections_and_start_here(store, noon):
    """A busy board renders the grounding line, the first step, and the buckets."""
    store.upsert_commitment(
        title="Dentist", start_at=_at(noon + timedelta(minutes=20)),
        lead_minutes=60, external_id="personal:1", hardness="hard",
    )
    store.add_todo("Submit report", deadline=noon.strftime("%Y-%m-%d"))
    text = render_panic(build_panic(store, now=noon))
    assert "# Panic mode" in text
    assert "Start here" in text
    assert "Already behind" in text
    assert "Bearing down soon" in text
    assert "actually need you right now" in text


class _FakeClient:
    def __init__(self, reply="", error=False, model="fake"):
        self.reply, self.error, self.model = reply, error, model

    def generate(self, prompt, *, system=None):
        if self.error:
            raise OllamaError("down")
        return self.reply


def test_summarize_panic_llm_and_fallback(store):
    """LLM prose when available; the deterministic triage on failure."""
    ok = summarize_panic(store, client=_FakeClient(reply="You're okay. Do this first."))
    assert ok.source == "llm" and "okay" in ok.text

    fb = summarize_panic(store, client=_FakeClient(error=True))
    assert fb.source == "heuristic" and "Panic mode" in fb.text


def test_summarize_panic_falls_back_on_non_ollama_provider_error(store):
    """A provider failure that isn't an OllamaError must still fall back.

    Regression: summarize_panic once caught only OllamaError, so if the panic
    agent were routed to Anthropic (e.g. ANTHROPIC_AGENTS=all) a request-time
    AnthropicError would escape instead of degrading to the deterministic triage.
    It now delegates to summarize_or_fallback, which catches the shared
    ProviderError base.
    """

    class _AnthropicDown:
        model = "claude"

        def generate(self, prompt, *, system=None):
            raise AnthropicError("429 overloaded")

    fb = summarize_panic(store, client=_AnthropicDown())
    assert fb.source == "heuristic" and "Panic mode" in fb.text


def test_panic_endpoint(store, noon):
    """GET /panic returns structured buckets + rendered text, token-guarded."""
    store.upsert_commitment(
        title="Dentist", start_at=_at(utcnow() + timedelta(minutes=5)),
        lead_minutes=60, external_id="personal:1", hardness="hard",
    )
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        assert c.get("/panic").status_code == 401
        body = c.get("/panic", headers={"X-Prefrontal-Token": SECRET}).json()
    assert body["late"][0]["title"] == "Leave for Dentist"
    assert body["first_step"]
    assert "Panic mode" in body["text"]


def test_headline_leads_with_first_step_and_reassures_when_clear(store, noon):
    """The one-line headline (for one-tap use) folds in the first step, or reassures."""
    assert "nothing" in build_panic(store, now=noon).headline.lower()  # clear board
    store.add_todo("Call the plumber", deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    hl = build_panic(store, now=noon).headline
    assert "Start here:" in hl and "needs you right now" in hl


def test_overwhelm_level_needs_a_real_pileup(store, noon):
    """One late thing is calm; two late, or late + a full plate, is overwhelmed."""
    # A single overdue todo — not a crisis.
    store.add_todo("A", deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    assert overwhelm_level(build_panic(store, now=noon)) == "calm"
    # Second overdue todo tips it over.
    store.add_todo("B", deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    plan = build_panic(store, now=noon)
    assert overwhelm_level(plan) == "overwhelmed"
    assert "2 already late" in panic_alert_message(plan, name="Tom")
    assert panic_alert_message(plan).startswith("Heads up —")


def _overwhelm(store, now):
    """Three overdue todos → a genuine pile-up (overwhelm_level == overwhelmed)."""
    for t in ("A", "B", "C"):
        store.add_todo(t, deadline=(now - timedelta(days=1)).strftime("%Y-%m-%d"))


def test_evaluate_panic_check_fires_on_edge_then_holds(store, noon):
    """The shared decision fires once on the calm→overwhelmed edge, then holds."""
    _overwhelm(store, noon)
    first = evaluate_panic_check(store, now=noon, quiet_hours=False)
    assert first.fire and first.level == "overwhelmed" and first.message and first.first_step
    second = evaluate_panic_check(store, now=noon, quiet_hours=False)
    assert second.fire is False and second.level == "overwhelmed"  # edge consumed


def test_evaluate_panic_check_defers_in_quiet_hours_preserving_the_edge(store, noon):
    """quiet_hours=True defers (no fire) without advancing last_panic_level, so the
    next responsive-hours check still fires."""
    _overwhelm(store, noon)
    deferred = evaluate_panic_check(store, now=noon, quiet_hours=True)
    assert deferred.fire is False and deferred.level == "overwhelmed"
    awake = evaluate_panic_check(store, now=noon, quiet_hours=False)
    assert awake.fire is True


def test_evaluate_panic_check_records_step_only_when_ackable(store, noon):
    """A pending 'panic' step (for the Did-it button) is recorded only when ackable."""
    store.set_state("panic_alert_cooldown_minutes", "0")  # allow back-to-back fires
    _overwhelm(store, noon)
    acked = evaluate_panic_check(store, now=noon, quiet_hours=False, ackable=True)
    assert acked.fire and acked.step_id is not None
    store.set_state("last_panic_level", "calm")  # re-arm the edge
    plain = evaluate_panic_check(store, now=noon, quiet_hours=False, ackable=False)
    assert plain.fire and plain.step_id is None


def test_panic_check_edge_triggers_then_stays_quiet(store, noon):
    """The proactive check fires once on the overwhelm edge, then not on every poll."""
    for t in ("A", "B", "C"):
        store.add_todo(t, deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    _disable_quiet_hours(store)
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        h = {"X-Prefrontal-Token": SECRET}
        first = c.post("/webhooks/panic/check", headers=h).json()
        assert first["fire"] is True and first["level"] == "overwhelmed"
        assert first["message"] and first["first_step"]
        # Same spike on the next poll → no repeat nudge.
        second = c.post("/webhooks/panic/check", headers=h).json()
        assert second["fire"] is False and second["level"] == "overwhelmed"


def test_panic_check_calm_does_not_fire(store, noon):
    """A calm plate never nudges."""
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        body = c.post("/webhooks/panic/check", headers={"X-Prefrontal-Token": SECRET}).json()
    assert body["fire"] is False and body["level"] == "calm"


def test_panic_check_fire_carries_first_step_inline_and_a_triage_button(store, noon):
    """A firing nudge has the first step in its message + a view button to /panic."""
    for t in ("A", "B", "C"):
        store.add_todo(t, deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    _disable_quiet_hours(store)
    settings = Settings(webhook_secret=SECRET, oauth_base_url="https://mac-mini.tailnet.ts.net")
    app = create_app(store=store, settings=settings)
    with TestClient(app) as c:
        body = c.post("/webhooks/panic/check", headers={"X-Prefrontal-Token": SECRET}).json()
    assert body["fire"] is True
    # First step is inline in the delivered message (no app switch needed to start).
    assert body["first_step"] and body["first_step"] in body["message"]
    # …and a single one-tap "view" button opens the full triage overlay.
    assert len(body["actions"]) == 1
    btn = body["actions"][0]
    assert btn["action"] == "view"
    assert btn["url"] == "https://mac-mini.tailnet.ts.net/dashboard?panic=1"


def test_panic_check_action_empty_without_public_origin(store, noon):
    """No public origin configured → the nudge still fires, just with no button."""
    for t in ("A", "B", "C"):
        store.add_todo(t, deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    _disable_quiet_hours(store)
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        body = c.post("/webhooks/panic/check", headers={"X-Prefrontal-Token": SECRET}).json()
    assert body["fire"] is True
    assert body["actions"] == []


def test_panic_check_defers_overwhelm_during_quiet_hours(store, noon):
    """Outside responsive hours an overwhelm spike is deferred, not dropped: it
    doesn't fire now, but the edge is preserved so the first poll back inside
    responsive hours still nudges (a 3am pile-up can't wake you, but isn't lost)."""
    from prefrontal.scheduling import local_hour_of

    for t in ("A", "B", "C"):
        store.add_todo(t, deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    # A responsive window that excludes the current (UTC) hour → we're "asleep".
    hour = local_hour_of(utcnow(), "UTC")
    store.set_state("responsive_hours_start", str((hour + 1) % 24))
    store.set_state("responsive_hours_end", str((hour + 2) % 24))
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET, timezone="UTC"))
    h = {"X-Prefrontal-Token": SECRET}
    with TestClient(app) as c:
        quiet = c.post("/webhooks/panic/check", headers=h).json()
        assert quiet["fire"] is False and quiet["level"] == "overwhelmed"
        # Edge preserved (last_panic_level not advanced): opening the window fires.
        _disable_quiet_hours(store)
        awake = c.post("/webhooks/panic/check", headers=h).json()
    assert awake["fire"] is True and awake["level"] == "overwhelmed"
    assert awake["message"] and awake["first_step"]


def test_panic_check_offers_did_it_button_and_captures_the_step(store, noon):
    """A firing nudge (with signing configured) logs a pending panic episode and a
    signed 'Did it' button; tapping it resolves the episode to a success."""
    for t in ("A", "B", "C"):
        store.add_todo(t, deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    _disable_quiet_hours(store)
    base = "https://mac-mini.tailnet.ts.net"
    settings = Settings(webhook_secret=SECRET, session_secret="sign-key", oauth_base_url=base)
    app = create_app(store=store, settings=settings)
    h = {"X-Prefrontal-Token": SECRET}
    with TestClient(app) as c:
        body = c.post("/webhooks/panic/check", headers=h).json()
        assert body["fire"] is True
        # "Did it" (http, one-tap) comes first, then the "Open triage" view button.
        assert [a["action"] for a in body["actions"]] == ["http", "view"]
        did = body["actions"][0]
        assert did["label"] == "✓ Did it"
        # A pending panic episode was logged (outcome not yet known).
        eps = store.episodes_by_type("panic")
        assert len(eps) == 1 and eps[0]["outcome"] is None
        # Tapping the signed button resolves it to a success.
        resp = c.get(did["url"].removeprefix(base))
    assert resp.status_code == 200
    eps = store.episodes_by_type("panic")
    assert eps[0]["outcome"] == "success" and eps[0]["acknowledged"]


def test_panic_check_sweeps_unanswered_first_step_to_miss(store, noon):
    """A first-step nudge left unanswered past its ack window is swept to a miss,
    so the drift signal reflects steps that didn't happen — not just the taps."""
    for t in ("A", "B", "C"):
        store.add_todo(t, deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    _disable_quiet_hours(store)
    store.set_state("panic_step_ack_window_minutes", "0")  # sweep on the next poll
    settings = Settings(
        webhook_secret=SECRET, session_secret="sign-key", oauth_base_url="https://x.ts.net"
    )
    app = create_app(store=store, settings=settings)
    h = {"X-Prefrontal-Token": SECRET}
    with TestClient(app) as c:
        first = c.post("/webhooks/panic/check", headers=h).json()
        assert first["fire"] is True
        assert store.episodes_by_type("panic")[0]["outcome"] is None
        # A later poll (edge already consumed, so it won't re-fire) sweeps the
        # unanswered step to a miss.
        c.post("/webhooks/panic/check", headers=h)
    ep = store.episodes_by_type("panic")[0]
    assert ep["outcome"] == "miss" and not ep["acknowledged"]


def test_panic_check_skips_step_capture_without_signing_key(store, noon):
    """Without a signing key the 'Did it' button can't be delivered, so no pending
    episode is logged — otherwise every un-ackable nudge would become a false miss."""
    for t in ("A", "B", "C"):
        store.add_todo(t, deadline=(noon - timedelta(days=1)).strftime("%Y-%m-%d"))
    _disable_quiet_hours(store)
    # oauth_base_url set (view button works) but no session_secret (no signed button).
    settings = Settings(webhook_secret=SECRET, oauth_base_url="https://x.ts.net")
    app = create_app(store=store, settings=settings)
    with TestClient(app) as c:
        body = c.post("/webhooks/panic/check", headers={"X-Prefrontal-Token": SECRET}).json()
    assert body["fire"] is True
    assert [a["action"] for a in body["actions"]] == ["view"]  # only the triage link
    assert store.episodes_by_type("panic") == []


def test_panic_step_outcomes_feed_the_drift_pattern(store):
    """Resolved panic first-step episodes produce a 'panic' drift pattern with no
    patterns.py changes — closing the overwhelm learning loop."""
    from prefrontal.memory.patterns import recompute_patterns

    store.log_episode("panic", outcome="success", acknowledged=True, context="a")
    store.log_episode("panic", outcome="miss", acknowledged=False, context="b")
    store.log_episode("panic", outcome="success", acknowledged=True, context="c")
    recompute_patterns(store)
    drift = [p for p in store.get_patterns("drift") if p["context_key"] == "panic"]
    assert len(drift) == 1
    # DRIFT_WEIGHTS: success→0, miss→1; mean of (0, 1, 0) = 1/3 off-track
    # (stored rounded to a few decimals).
    assert abs(drift[0]["observed_value"] - (1 / 3)) < 1e-3


def test_dashboard_deep_link_auto_opens_the_triage(store):
    """The dashboard honors ?panic=1 (the nudge's deep link) by opening the overlay."""
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        dash = c.get("/dashboard").text
    assert 'get("panic") === "1"' in dash  # reads the deep-link param
    assert "openPanic()" in dash            # …and opens the overlay on auth


def test_dashboard_wires_the_panic_button(store):
    """The dashboard shell exposes a panic entry point backed by /panic.

    (The shared household surfaces are the calm sheet views — they no longer
    carry the per-user panic overlay; that lives on the dashboard.)
    """
    app = create_app(store=store, settings=Settings(webhook_secret=SECRET))
    with TestClient(app) as c:
        dash = c.get("/dashboard").text
        household = c.get("/household").text
    assert 'id="panic-btn"' in dash
    assert 'id="panic"' in dash  # the overlay
    assert '"/panic"' in dash    # the fetch call
    # The Household hub is the shared sheet — no panic button, reads the sheet.
    assert 'id="panic-btn"' not in household
    assert "/household/sheet" in household
