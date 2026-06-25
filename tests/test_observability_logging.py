"""Structured JSON logging + correlation ids + event sink (Phase 4.10.4 + 10.2)."""

from __future__ import annotations

import json
import logging

import pytest

from colabctl.observability import (
    JsonFormatter,
    _CorrelationFilter,
    configure_logging,
    correlation_context,
    get_logger,
    set_event_sink,
)


def _record(msg: str = "hi") -> logging.LogRecord:
    return logging.LogRecord("colabctl.test", logging.INFO, "f", 1, msg, None, None)


@pytest.fixture
def clean_logger():
    """Snapshot/restore the colabctl root logger so configure_logging doesn't leak handlers."""
    root = logging.getLogger("colabctl")
    before, level = list(root.handlers), root.level
    yield
    root.handlers = before
    root.setLevel(level)
    set_event_sink(None)


def test_json_formatter_emits_structured_line():
    rec = _record("hello")
    _CorrelationFilter().filter(rec)
    out = json.loads(JsonFormatter().format(rec))
    assert out["level"] == "INFO" and out["logger"] == "colabctl.test"
    assert out["message"] == "hello" and "ts" in out


def test_correlation_context_attaches_ids_and_drops_none():
    rec = _record()
    with correlation_context(job_id="colab-abc", incarnation="2", backend=None):
        _CorrelationFilter().filter(rec)
        out = json.loads(JsonFormatter().format(rec))
    assert out["job_id"] == "colab-abc" and out["incarnation"] == "2"
    assert "backend" not in out  # None-valued fields are dropped


def test_correlation_context_resets_on_exit():
    with correlation_context(job_id="x"):
        pass
    rec = _record()
    _CorrelationFilter().filter(rec)
    assert "job_id" not in json.loads(JsonFormatter().format(rec))


def test_event_sink_receives_structured_payloads(clean_logger):
    events: list[dict] = []
    set_event_sink(events.append)
    configure_logging(level=logging.INFO)
    with correlation_context(job_id="job-1"):
        get_logger("sink-test").info("via sink")
    assert any(e.get("job_id") == "job-1" and e["message"] == "via sink" for e in events)


def test_json_logs_opt_in_writes_json(clean_logger, capsys):
    configure_logging(level=logging.INFO, json_logs=True)
    get_logger("json-test").info("structured")
    err = capsys.readouterr().err
    line = next(ln for ln in err.splitlines() if "structured" in ln)
    assert json.loads(line)["message"] == "structured"


def test_configure_logging_respects_env(clean_logger, monkeypatch, capsys):
    monkeypatch.setenv("COLABCTL_LOG_JSON", "1")
    configure_logging(level=logging.INFO)
    get_logger("env-test").warning("env-json")
    line = next(ln for ln in capsys.readouterr().err.splitlines() if "env-json" in ln)
    assert json.loads(line)["level"] == "WARNING"
