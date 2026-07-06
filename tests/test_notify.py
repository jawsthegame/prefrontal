"""Tests for the ntfy interactive-nudge action builder (webhooks/notify.py).

Pure: builds signed one-tap action-button dicts for a nudge, and returns nothing
when the public origin / signing key isn't configured.
"""

from __future__ import annotations

from prefrontal.webhooks.notify import (
    act_url,
    alarm_actions,
    alarm_actions_for_cue,
    nudge_actions,
    panic_actions,
)
from prefrontal.webhooks.oauth import verify_action

SIGNING = "notify-signing-key"
BASE = "https://mac-mini.tailnet.ts.net"


def test_act_url_signs_and_round_trips():
    url = act_url(BASE, "tom", "focus_end", 42, SIGNING)
    assert url.startswith(f"{BASE}/nudge/act?t=")
    token = url.split("t=", 1)[1]
    assert verify_action(token, SIGNING) == ("tom", "focus_end", 42)


def test_act_url_empty_when_unconfigured():
    assert act_url("", "tom", "focus_end", 1, SIGNING) == ""
    assert act_url(BASE, "tom", "focus_end", 1, "") == ""


def test_nudge_actions_per_kind():
    """Each nudge kind offers its own labelled buttons, all signed http GETs."""
    outing = nudge_actions("outing", 7, base_url=BASE, secret=SIGNING, handle="tom")
    assert [b["label"] for b in outing] == ["I'm back", "Abandon"]
    assert all(b["action"] == "http" and b["method"] == "GET" for b in outing)
    assert verify_action(outing[0]["url"].split("t=", 1)[1], SIGNING) == (
        "tom", "outing_return", 7,
    )
    assert verify_action(outing[1]["url"].split("t=", 1)[1], SIGNING) == (
        "tom", "outing_abandon", 7,
    )

    focus = nudge_actions("focus", 3, base_url=BASE, secret=SIGNING, handle="tom")
    assert [b["label"] for b in focus] == ["Wrap up"]

    dep = nudge_actions("departure", 9, base_url=BASE, secret=SIGNING, handle="tom")
    assert [b["label"] for b in dep] == ["Made it", "Missed it"]

    pause = nudge_actions("pause", 5, base_url=BASE, secret=SIGNING, handle="tom")
    assert [b["label"] for b in pause] == ["Stay on task", "Park it", "Switch anyway"]
    assert verify_action(pause[2]["url"].split("t=", 1)[1], SIGNING) == (
        "tom", "switch_switch", 5,
    )


def test_nudge_actions_empty_when_unconfigured_or_unknown():
    # No origin / secret → no buttons (feature simply off).
    assert nudge_actions("outing", 7, base_url="", secret=SIGNING, handle="tom") == []
    assert nudge_actions("outing", 7, base_url=BASE, secret="", handle="tom") == []
    # No target, or a kind with no buttons → empty.
    assert nudge_actions("outing", None, base_url=BASE, secret=SIGNING, handle="tom") == []
    assert nudge_actions("mystery", 1, base_url=BASE, secret=SIGNING, handle="tom") == []


def test_panic_actions_open_the_triage_overlay():
    """A single unsigned `view` button that deep-links to the dashboard overlay."""
    actions = panic_actions(BASE)
    assert len(actions) == 1
    btn = actions[0]
    assert btn["action"] == "view"       # opens a page, fires no request → no signing
    assert btn["label"] == "Open triage"
    assert btn["url"] == f"{BASE}/dashboard?panic=1"
    # No public origin → no button (feature simply off), like the signed buttons.
    assert panic_actions("") == []


def test_alarm_actions_deep_link_to_the_shortcut():
    """An unsigned `view` button opening the iOS Shortcuts URL scheme with the time."""
    actions = alarm_actions("Set Alarm", "07:15")
    assert len(actions) == 1
    btn = actions[0]
    assert btn["action"] == "view"  # opens the Shortcuts app, no server round-trip
    assert btn["label"] == "⏰ Set alarm"
    assert btn["url"] == "shortcuts://run-shortcut?name=Set%20Alarm&input=text&text=07%3A15"


def test_alarm_actions_empty_without_name_or_time():
    assert alarm_actions("", "07:15") == []
    assert alarm_actions("Set Alarm", "") == []


class _Cue:
    def __init__(self, ref):
        self.ref = ref


def test_alarm_actions_for_cue_reads_ref():
    """The button is built from the cue's ref (alarm_shortcut + alarm_at)."""
    actions = alarm_actions_for_cue(_Cue({"alarm_shortcut": "Wake Up", "alarm_at": "06:45"}))
    assert len(actions) == 1
    assert actions[0]["url"] == "shortcuts://run-shortcut?name=Wake%20Up&input=text&text=06%3A45"
    # Missing payload → no button (a plain push).
    assert alarm_actions_for_cue(_Cue({})) == []
