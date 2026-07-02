"""Tests for the ntfy interactive-nudge action builder (webhooks/notify.py).

Pure: builds signed one-tap action-button dicts for a nudge, and returns nothing
when the public origin / signing key isn't configured.
"""

from __future__ import annotations

from prefrontal.webhooks.notify import act_url, nudge_actions
from prefrontal.webhooks.oauth import verify_action

SIGNING = "notify-signing-key"
BASE = "https://agent-1.tail8b0a.ts.net"


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


def test_nudge_actions_empty_when_unconfigured_or_unknown():
    # No origin / secret → no buttons (feature simply off).
    assert nudge_actions("outing", 7, base_url="", secret=SIGNING, handle="tom") == []
    assert nudge_actions("outing", 7, base_url=BASE, secret="", handle="tom") == []
    # No target, or a kind with no buttons → empty.
    assert nudge_actions("outing", None, base_url=BASE, secret=SIGNING, handle="tom") == []
    assert nudge_actions("mystery", 1, base_url=BASE, secret=SIGNING, handle="tom") == []
