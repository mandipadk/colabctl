"""CLI wiring for the detached `colabctl job` commands.

A real ``DetachedColabBackend`` over ``LocalExecTransport`` is injected so the commands
drive actual (local-subprocess) jobs — covering submit → status → logs → result → cancel
through the Typer surface.
"""

from __future__ import annotations

import time
from pathlib import Path

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.jobs.backend import DetachedColabBackend
from colabctl.state import StateStore
from conftest import LocalExecTransport

runner = CliRunner()


def _inject(monkeypatch, tmp_path: Path) -> StateStore:
    store = StateStore(home=tmp_path / "home")
    root = str(tmp_path / "jobs")

    def factory(state):
        return DetachedColabBackend(
            LocalExecTransport(), state=store, root=root, poll_interval=0.05
        )

    monkeypatch.setattr(cli_mod, "_make_detached_backend", factory)
    return store


def _submit(code: str = "print('cli detached')") -> str:
    result = runner.invoke(cli_mod.app, ["job", "run", "-c", code, "--detach"])
    assert result.exit_code == 0, result.output
    return result.output.splitlines()[0].strip()  # first line is the job id


def _await_done(job_id: str, *, tries: int = 200) -> None:
    for _ in range(tries):
        out = runner.invoke(cli_mod.app, ["job", "status", job_id]).output
        if any(s in out for s in ("SUCCEEDED", "FAILED", "CANCELLED")):
            return
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished; last status: {out}")


def test_detach_submit_then_status_logs_result(monkeypatch, tmp_path) -> None:
    _inject(monkeypatch, tmp_path)
    job_id = _submit("print('cli detached output')")
    assert job_id.startswith("colab-")

    _await_done(job_id)
    logs = runner.invoke(cli_mod.app, ["job", "logs", job_id])
    assert "cli detached output" in logs.output

    result = runner.invoke(cli_mod.app, ["job", "result", job_id])
    assert result.exit_code == 0
    assert "SUCCEEDED" in result.output


def test_detach_failure_exits_nonzero(monkeypatch, tmp_path) -> None:
    _inject(monkeypatch, tmp_path)
    job_id = _submit("import sys; sys.exit(5)")
    _await_done(job_id)
    result = runner.invoke(cli_mod.app, ["job", "result", job_id])
    assert result.exit_code == 1
    assert "FAILED" in result.output


def test_job_list_shows_submitted_jobs(monkeypatch, tmp_path) -> None:
    _inject(monkeypatch, tmp_path)
    a = _submit("print('a')")
    b = _submit("print('b')")
    listed = runner.invoke(cli_mod.app, ["job", "list"])
    assert a in listed.output and b in listed.output


def test_detach_rejected_for_non_colab_backend(monkeypatch, tmp_path) -> None:
    _inject(monkeypatch, tmp_path)
    result = runner.invoke(
        cli_mod.app, ["job", "run", "-c", "print(1)", "--detach", "--backend", "modal"]
    )
    assert result.exit_code == 2
    assert "only supported for the colab backend" in result.output


def test_cancel_marks_cancelled(monkeypatch, tmp_path) -> None:
    _inject(monkeypatch, tmp_path)
    job_id = _submit("import time; time.sleep(60)")
    # Wait until running.
    for _ in range(200):
        if "RUNNING" in runner.invoke(cli_mod.app, ["job", "status", job_id]).output:
            break
        time.sleep(0.05)
    assert runner.invoke(cli_mod.app, ["job", "cancel", job_id]).exit_code == 0
    assert "CANCELLED" in runner.invoke(cli_mod.app, ["job", "status", job_id]).output
