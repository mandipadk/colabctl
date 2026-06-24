"""Cross-backend USD spend ledger in the state store (Phase 2 cost-engine foundation)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from colabctl.models import Accelerator
from colabctl.state import SpendRecord, StateStore, utcnow


def test_record_and_total_spend(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    store.record_spend(
        SpendRecord(backend="modal", accelerator=Accelerator.A100, est_cost_usd=3.95, hours=1.0)
    )
    store.record_spend(SpendRecord(backend="colab", accelerator=Accelerator.T4, est_cost_usd=0.0))
    store.record_spend(
        SpendRecord(backend="modal", accelerator=Accelerator.H100, est_cost_usd=5.59, hours=1.0)
    )
    assert store.total_spend_usd() == pytest.approx(9.54)
    assert len(store.list_spend()) == 3
    assert {r.backend for r in store.list_spend()} == {"modal", "colab"}


def test_total_spend_since(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    store.record_spend(
        SpendRecord(at=utcnow() - timedelta(days=2), backend="modal", est_cost_usd=10.0)
    )
    store.record_spend(SpendRecord(backend="modal", est_cost_usd=2.0))  # ~now
    cutoff = utcnow() - timedelta(days=1)
    assert store.total_spend_usd(since=cutoff) == pytest.approx(2.0)  # only the recent one
    assert store.total_spend_usd() == pytest.approx(12.0)


def test_spend_ledger_is_backward_compatible(tmp_path: Path) -> None:
    # An old state.json with no `spend` field still loads (additive, optional).
    store = StateStore(home=tmp_path / "h")
    (tmp_path / "h").mkdir(parents=True)
    (tmp_path / "h" / "state.json").write_text('{"schema_version": 1, "sessions": {}, "jobs": {}}')
    assert store.total_spend_usd() == 0.0
    assert store.list_spend() == []
