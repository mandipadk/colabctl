"""colabctl doctor — preflight health checks (Phase 4.10.3)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.doctor import (
    Check,
    _adc_check,
    _backends_check,
    _state_store_check,
    overall_status,
    run_checks,
)

runner = CliRunner()


def test_run_checks_returns_all_named_checks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    checks = run_checks(home=tmp_path / "h")
    assert {c.name for c in checks} == {
        "auth-adc",
        "colab-binary",
        "backends",
        "state-store",
        "agent-skill",
    }
    assert all(c.status in ("ok", "warn", "fail") for c in checks)


def test_overall_status_is_worst():
    assert overall_status([Check("a", "ok", ""), Check("b", "warn", "")]) == "warn"
    assert overall_status([Check("a", "ok", ""), Check("b", "fail", "")]) == "fail"
    assert overall_status([Check("a", "ok", "")]) == "ok"


def test_adc_warns_when_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert _adc_check().status == "warn"


def test_state_store_warns_on_quarantined_doc(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / "state.json.corrupt-20260101T000000").write_text("garbage")
    check = _state_store_check(home)
    assert check.status == "warn" and "corrupt" in check.detail


def test_backends_check_lists_configured(monkeypatch) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "rp-key")
    check = _backends_check()
    assert check.status == "ok"
    assert "runpod" in check.detail and "colab" in check.detail


def test_cli_doctor_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COLABCTL_HOME", str(tmp_path / "h"))
    result = runner.invoke(cli_mod.app, ["doctor"])
    assert result.exit_code == 0  # no check returns "fail" → never errors
    assert "doctor:" in result.output
    assert "auth-adc" in result.output and "colab-binary" in result.output


async def test_mcp_health_tool():
    from colabctl.mcp_server import health_check

    out = await health_check()
    assert out["status"] in ("ok", "warn", "fail")
    assert {c["name"] for c in out["checks"]} >= {"auth-adc", "backends", "state-store"}
