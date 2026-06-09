"""Adversarial tests for the Typer CLI: session commands, error exit codes, helpers."""

from __future__ import annotations

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.errors import TransportError
from colabctl.models import (
    Accelerator,
    ExecutionResult,
    SessionInfo,
    SessionStatus,
    StreamOutput,
    Variant,
)
from colabctl.sdk.client import ColabClient
from conftest import FakeTransport

runner = CliRunner()


def _patch(monkeypatch, transport):
    monkeypatch.setattr(cli_mod, "_make_client", lambda state: ColabClient(transport=transport))


# --- _fmt_session pure formatting -------------------------------------------


def test_fmt_session_omits_unknown_status():
    info = SessionInfo(name="a", endpoint="ep", accelerator=Accelerator.T4, variant=Variant.GPU)
    line = cli_mod._fmt_session(info)
    assert "[a] ep" in line and "Hardware: T4" in line and "Variant: GPU" in line
    assert "Status:" not in line  # UNKNOWN is omitted


def test_fmt_session_includes_known_status_and_cpu_label():
    info = SessionInfo(
        name="a",
        endpoint="ep",
        accelerator=Accelerator.NONE,
        variant=Variant.DEFAULT,
        status=SessionStatus.IDLE,
    )
    line = cli_mod._fmt_session(info)
    assert "Hardware: CPU" in line
    assert "Status: IDLE" in line


# --- version ----------------------------------------------------------------


def test_version_command():
    result = runner.invoke(cli_mod.app, ["version"])
    assert result.exit_code == 0
    assert "colabctl" in result.output


# --- exec -------------------------------------------------------------------


def test_exec_prints_output(monkeypatch):
    _patch(monkeypatch, FakeTransport())
    result = runner.invoke(cli_mod.app, ["exec", "-s", "sess", "--code", "print(1)"])
    assert result.exit_code == 0
    assert "ran:print(1)" in result.output


def test_exec_failure_exits_one(monkeypatch):
    class ErrTransport(FakeTransport):
        async def execute(self, name, code, *, timeout=None, on_output=None):
            return ExecutionResult(
                status="error", outputs=[StreamOutput(name="stderr", text="boom")]
            )

    _patch(monkeypatch, ErrTransport())
    result = runner.invoke(cli_mod.app, ["exec", "-s", "sess", "--code", "boom"])
    assert result.exit_code == 1


# --- sessions / status / stop / new -----------------------------------------


def test_sessions_empty(monkeypatch):
    _patch(monkeypatch, FakeTransport())
    result = runner.invoke(cli_mod.app, ["sessions"])
    assert result.exit_code == 0
    assert "No active sessions." in result.output


def test_sessions_lists_items(monkeypatch):
    t = FakeTransport()
    t.sessions["a"] = SessionInfo(
        name="a", endpoint="ep", accelerator=Accelerator.T4, variant=Variant.GPU
    )
    _patch(monkeypatch, t)
    result = runner.invoke(cli_mod.app, ["sessions"])
    assert result.exit_code == 0
    assert "[a] ep" in result.output


def test_status_not_found(monkeypatch):
    _patch(monkeypatch, FakeTransport())
    result = runner.invoke(cli_mod.app, ["status", "ghost"])
    assert result.exit_code == 0
    assert "not found" in result.output


def test_stop_command(monkeypatch):
    t = FakeTransport()
    t.sessions["a"] = SessionInfo(name="a", endpoint="ep")
    _patch(monkeypatch, t)
    result = runner.invoke(cli_mod.app, ["stop", "a"])
    assert result.exit_code == 0
    assert "Stopped a." in result.output
    assert "a" in t.stopped


def test_new_command(monkeypatch):
    _patch(monkeypatch, FakeTransport())
    result = runner.invoke(cli_mod.app, ["new", "--gpu", "T4", "--name", "mysess"])
    assert result.exit_code == 0
    assert "mysess" in result.output


# --- error path: ColabctlError -> red error + exit 1 ------------------------


def test_colabctl_error_exits_one(monkeypatch):
    class RaisingTransport(FakeTransport):
        async def list_sessions(self):
            raise TransportError("no auth on this account")

    _patch(monkeypatch, RaisingTransport())
    result = runner.invoke(cli_mod.app, ["sessions"])
    assert result.exit_code == 1
    assert "error:" in result.output


# --- job run: both FILE and --code -> usage error (exit 2) ------------------


def test_job_run_both_file_and_code_exits_two(tmp_path):
    f = tmp_path / "script.py"
    f.write_text("x = 1\n")
    result = runner.invoke(cli_mod.app, ["job", "run", str(f), "-c", "y=2", "--backend", "modal"])
    assert result.exit_code == 2


# --- unknown gpu surfaces as an error ---------------------------------------


def test_run_unknown_gpu_exits_one(monkeypatch, tmp_path):
    f = tmp_path / "s.py"
    f.write_text("print(1)\n")
    _patch(monkeypatch, FakeTransport())
    result = runner.invoke(cli_mod.app, ["run", str(f), "--gpu", "rtx9090"])
    assert result.exit_code == 1
    assert "error:" in result.output
