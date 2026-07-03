"""Tests for the logging seam (prefrontal.log)."""
from __future__ import annotations

import logging

import prefrontal.log as log_module
from prefrontal.log import configure_logging, get_logger


def test_get_logger_returns_named_logger():
    assert get_logger("prefrontal.something").name == "prefrontal.something"


def test_configure_logging_is_idempotent(monkeypatch):
    # Reset the module guard so we can observe the first call taking effect.
    monkeypatch.setattr(log_module, "_configured", False)
    calls: list[dict] = []
    monkeypatch.setattr(
        logging, "basicConfig", lambda **kwargs: calls.append(kwargs)
    )
    configure_logging(level="DEBUG")
    configure_logging(level="DEBUG")  # second call is a no-op
    assert len(calls) == 1
    assert calls[0]["level"] == logging.DEBUG


def test_configure_logging_reads_env(monkeypatch):
    monkeypatch.setattr(log_module, "_configured", False)
    monkeypatch.setenv("PREFRONTAL_LOG_LEVEL", "warning")
    captured: dict = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: captured.update(kwargs))
    configure_logging()
    assert captured["level"] == logging.WARNING
