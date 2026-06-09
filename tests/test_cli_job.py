"""Tests for the `colabctl job` CLI commands (fake backend injected)."""

from __future__ import annotations

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.backends.base import JobResult, JobState
from conftest import FakeBackend

runner = CliRunner()


def test_job_run_prints_stdout_and_passes_spec(monkeypatch):
    fb = FakeBackend(
        name="modal",
        result=JobResult(id="j", backend="modal", state=JobState.SUCCEEDED, stdout="hello\n"),
    )
    monkeypatch.setattr(cli_mod, "_make_backend", lambda name, state: fb)
    result = runner.invoke(
        cli_mod.app, ["job", "run", "-c", "print(1)", "--backend", "modal", "--gpu", "A100"]
    )
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert fb.specs and fb.specs[0].accelerator.value == "A100"
    assert fb.closed  # backend released


def test_job_run_failure_exits_nonzero(monkeypatch):
    fb = FakeBackend(result=JobResult(id="j", backend="modal", state=JobState.FAILED, error="boom"))
    monkeypatch.setattr(cli_mod, "_make_backend", lambda name, state: fb)
    result = runner.invoke(cli_mod.app, ["job", "run", "-c", "x", "--backend", "modal"])
    assert result.exit_code == 1


def test_job_run_requires_exactly_one_source():
    # Neither FILE nor --code → usage error (exit 2), no backend built.
    result = runner.invoke(cli_mod.app, ["job", "run", "--backend", "modal"])
    assert result.exit_code == 2


def test_job_run_cpu_with_none_gpu(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(cli_mod, "_make_backend", lambda name, state: fb)
    result = runner.invoke(cli_mod.app, ["job", "run", "-c", "x=1", "--gpu", "none"])
    assert result.exit_code == 0
    assert fb.specs[0].accelerator.value == "NONE"


def test_job_backends_lists_all(monkeypatch):
    monkeypatch.setattr(cli_mod, "_make_backend", lambda name, state: FakeBackend(name=name))
    result = runner.invoke(cli_mod.app, ["job", "backends"])
    assert result.exit_code == 0
    for name in ("colab", "modal", "vertex"):
        assert name in result.stdout
