"""Append-only lifecycle+cost audit ledger (Phase 4.10.1)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.state import AuditEvent, StateStore, utcnow

runner = CliRunner()


def test_record_list_and_cost(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    store.record_audit(AuditEvent(action="submit", backend="colab", job_id="j1"))
    store.record_audit(AuditEvent(action="run", backend="modal", job_id="j2", cost_usd=2.5))
    store.record_audit(AuditEvent(action="resume", backend="colab", job_id="j1", incarnation=2))

    assert len(store.list_audit()) == 3
    assert [e.action for e in store.list_audit(job_id="j1")] == ["submit", "resume"]
    assert store.audit_cost_usd() == pytest.approx(2.5)
    assert store.audit_cost_usd(job_id="j1") == 0.0  # j1's events carry no cost


def test_since_filter(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    store.record_audit(AuditEvent(at=utcnow() - timedelta(days=3), action="run", cost_usd=9.0))
    store.record_audit(AuditEvent(action="run", cost_usd=1.0))
    cutoff = utcnow() - timedelta(days=1)
    assert store.audit_cost_usd(since=cutoff) == pytest.approx(1.0)
    assert len(store.list_audit(since=cutoff)) == 1


def test_audit_ledger_is_backward_compatible(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    (tmp_path / "h").mkdir(parents=True)
    (tmp_path / "h" / "state.json").write_text('{"schema_version": 1, "sessions": {}, "jobs": {}}')
    assert store.list_audit() == []  # old doc with no `audit` field loads fine


def test_cli_audit_shows_events_and_cost(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "h"
    monkeypatch.setenv("COLABCTL_HOME", str(home))
    StateStore(home=home).record_audit(
        AuditEvent(action="run", backend="modal", job_id="abc123", cost_usd=2.5, detail="SUCCEEDED")
    )
    result = runner.invoke(cli_mod.app, ["audit"])
    assert result.exit_code == 0
    assert "run" in result.output and "abc123" in result.output
    assert "$2.50" in result.output and "1 event(s)" in result.output
