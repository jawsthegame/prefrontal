"""Tests for prefrontal.config — numeric env-var hardening.

A fat-fingered numeric env var should degrade to the default (matching the
tolerant string parsers here), not raise a ValueError that takes the whole
process down at startup.
"""
from __future__ import annotations

from prefrontal.config import _float_env, _int_env, load_settings


def _fresh(tmp_path):
    """load_settings pointed at an absent dotenv, so only os.environ is read."""
    return load_settings(str(tmp_path / "absent.env"))


def test_int_env_and_float_env_degrade_on_garbage(monkeypatch):
    monkeypatch.setenv("X_INT", "eight")
    monkeypatch.setenv("X_FLOAT", "1.2.3")
    assert _int_env("X_INT", 8000) == 8000
    assert _float_env("X_FLOAT", 30.0) == 30.0
    # Absent and blank also fall back.
    monkeypatch.setenv("X_BLANK", "   ")
    assert _int_env("X_BLANK", 5) == 5
    assert _int_env("X_MISSING", 7) == 7
    # Valid values still parse (whitespace-trimmed).
    monkeypatch.setenv("X_INT", " 9090 ")
    monkeypatch.setenv("X_FLOAT", "1.5")
    assert _int_env("X_INT", 8000) == 9090
    assert _float_env("X_FLOAT", 30.0) == 1.5


def test_load_settings_survives_malformed_numeric_envs(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFRONTAL_PORT", "eight")
    monkeypatch.setenv("PREFRONTAL_CALENDAR_HORIZON_DAYS", "soon")
    monkeypatch.setenv("PREFRONTAL_TRIAGE_QUICK_DROP_DAYS", "two")
    monkeypatch.setenv("PREFRONTAL_TRIAGE_REPEAT_THRESHOLD", "lots")
    monkeypatch.setenv("PREFRONTAL_TRIAGE_DROP", "high")
    s = _fresh(tmp_path)  # must not raise
    assert s.port == 8000
    assert s.calendar_horizon_days == 30.0
    assert s.triage_quick_drop_days == 2.0
    assert s.triage_repeat_threshold == 2
    assert s.triage_drop_threshold == 0.0


def test_load_settings_reads_valid_numeric_envs(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFRONTAL_PORT", "9090")
    monkeypatch.setenv("PREFRONTAL_TRIAGE_REPEAT_THRESHOLD", "5")
    monkeypatch.setenv("PREFRONTAL_CALENDAR_HORIZON_DAYS", "14")
    s = _fresh(tmp_path)
    assert s.port == 9090
    assert s.triage_repeat_threshold == 5
    assert s.calendar_horizon_days == 14.0
