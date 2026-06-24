"""`colabctl cost` (dry-run price estimator) + `colabctl spend` (USD ledger)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.models import Accelerator
from colabctl.state import SpendRecord, StateStore

runner = CliRunner()


def test_cost_lists_backends_cheapest_first() -> None:
    result = runner.invoke(cli_mod.app, ["cost", "--gpu", "A100"])
    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if "$" in ln]
    # cheapest-first: colab ($1.50) before modal ($2.50) before vertex ($3.67)
    providers = [ln.split()[0] for ln in lines]
    assert providers.index("colab") < providers.index("modal") < providers.index("vertex")


def test_cost_spot_shows_only_spot_backends() -> None:
    result = runner.invoke(cli_mod.app, ["cost", "--gpu", "A100", "--spot"])
    assert result.exit_code == 0
    assert "runpod" in result.output  # only runpod has a spot A100 in the static table
    assert "spot" in result.output


def test_cost_allow_restricts_backends() -> None:
    result = runner.invoke(cli_mod.app, ["cost", "--gpu", "A100", "--allow", "modal,vertex"])
    assert result.exit_code == 0
    assert "modal" in result.output and "vertex" in result.output
    assert "colab" not in result.output


def test_spend_reports_ledger(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "h"
    monkeypatch.setenv("COLABCTL_HOME", str(home))
    store = StateStore(home=home)
    store.record_spend(
        SpendRecord(backend="modal", accelerator=Accelerator.A100, est_cost_usd=2.50)
    )
    store.record_spend(
        SpendRecord(backend="runpod", accelerator=Accelerator.H100, est_cost_usd=1.75)
    )

    result = runner.invoke(cli_mod.app, ["spend"])
    assert result.exit_code == 0
    assert "$4.25" in result.output  # total
    assert "2 allocation(s)" in result.output
    assert "modal" in result.output and "runpod" in result.output


def test_spend_empty_ledger(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COLABCTL_HOME", str(tmp_path / "empty"))
    result = runner.invoke(cli_mod.app, ["spend"])
    assert result.exit_code == 0
    assert "$0.00" in result.output and "0 allocation(s)" in result.output


def test_job_run_records_spend_to_ledger(tmp_path: Path, monkeypatch) -> None:
    from colabctl.backends.router import BackendRouter
    from conftest import FakeBackend

    home = tmp_path / "h"
    monkeypatch.setenv("COLABCTL_HOME", str(home))
    monkeypatch.setattr(
        cli_mod,
        "_make_router",
        lambda names, state: BackendRouter([FakeBackend("modal", accels=["A100"])]),
    )
    result = runner.invoke(
        cli_mod.app,
        ["job", "run", "-c", "print(1)", "--backend", "modal", "--allow", "modal", "--gpu", "A100"],
    )
    assert result.exit_code == 0, result.output
    assert "[modal] SUCCEEDED" in result.output
    # the run appended an estimated SpendRecord for the backend that ran
    spend = runner.invoke(cli_mod.app, ["spend"])
    assert "1 allocation(s)" in spend.output
    assert "modal" in spend.output and "A100" in spend.output


def test_job_run_budget_refuses_fail_closed(tmp_path: Path, monkeypatch) -> None:
    from colabctl.backends.router import BackendRouter
    from colabctl.state import SpendRecord, StateStore
    from conftest import FakeBackend

    home = tmp_path / "h"
    monkeypatch.setenv("COLABCTL_HOME", str(home))
    StateStore(home=home).record_spend(SpendRecord(backend="modal", est_cost_usd=9.50))  # near cap

    ran = {"count": 0}

    class _CountingBackend(FakeBackend):
        async def run(self, spec):  # type: ignore[override]
            ran["count"] += 1
            return await super().run(spec)

    monkeypatch.setattr(
        cli_mod,
        "_make_router",
        lambda names, state: BackendRouter([_CountingBackend("modal", accels=["A100"])]),
    )
    # modal A100 is $2.50/hr; 9.50 spent + 2.50 = 12.0 > $10 budget → refuse, never launch
    result = runner.invoke(
        cli_mod.app,
        [
            "job",
            "run",
            "-c",
            "print(1)",
            "--backend",
            "modal",
            "--allow",
            "modal",
            "--gpu",
            "A100",
            "--budget",
            "10",
        ],
    )
    assert result.exit_code == 1
    assert "budget" in result.output.lower()
    assert ran["count"] == 0  # fail-closed: nothing was launched
