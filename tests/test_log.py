"""Tests for the logging seam (prefrontal.log)."""
from __future__ import annotations

import logging

import pytest

import prefrontal.log as log_module
from prefrontal.log import configure_logging, get_logger


@pytest.fixture
def clean_prefrontal_logger(monkeypatch):
    """Isolate global logging state: reset the 'prefrontal' logger + the guard,
    and restore both afterwards so these tests don't leak handlers into others."""
    pf = logging.getLogger("prefrontal")
    saved_handlers, saved_level, saved_propagate = (
        list(pf.handlers),
        pf.level,
        pf.propagate,
    )
    pf.handlers.clear()
    pf.setLevel(logging.NOTSET)
    pf.propagate = True
    monkeypatch.setattr(log_module, "_configured", False)
    try:
        yield pf
    finally:
        pf.handlers[:] = saved_handlers
        pf.setLevel(saved_level)
        pf.propagate = saved_propagate


def test_get_logger_returns_named_logger():
    assert get_logger("prefrontal.something").name == "prefrontal.something"


def test_configure_logging_configures_the_prefrontal_namespace(clean_prefrontal_logger):
    """A handler lands on the 'prefrontal' logger with our format, and propagation
    is stopped so records don't also hit the host's root handlers."""
    pf = clean_prefrontal_logger
    configure_logging(level="DEBUG")
    assert len(pf.handlers) == 1
    assert pf.level == logging.DEBUG
    assert pf.propagate is False
    assert pf.handlers[0].formatter._fmt == log_module._LOG_FORMAT


def test_configure_logging_works_even_when_root_has_handlers(clean_prefrontal_logger):
    """Regression: logging.basicConfig no-ops once root has handlers (uvicorn/
    gunicorn install their own), so configuring root would silently do nothing.
    Configuring our own namespace still installs the handler in that case."""
    pf = clean_prefrontal_logger
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)  # simulate uvicorn having already configured root
    try:
        configure_logging(level="INFO")
        assert len(pf.handlers) == 1  # our handler landed despite root being configured
        assert pf.level == logging.INFO
    finally:
        root.removeHandler(sentinel)


def test_configure_logging_is_idempotent(clean_prefrontal_logger):
    """A second call adds no further handler (safe for multiple entry points)."""
    pf = clean_prefrontal_logger
    configure_logging(level="DEBUG")
    configure_logging(level="DEBUG")
    assert len(pf.handlers) == 1


def test_configure_logging_reads_env(clean_prefrontal_logger, monkeypatch):
    monkeypatch.setenv("PREFRONTAL_LOG_LEVEL", "warning")
    configure_logging()
    assert clean_prefrontal_logger.level == logging.WARNING
