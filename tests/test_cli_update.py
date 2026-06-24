"""`colabctl update` — self-upgrade to the latest PyPI version."""

from __future__ import annotations

from typer.testing import CliRunner

from colabctl import cli as cli_mod

runner = CliRunner()


def test_upgrade_command_pip_and_uv() -> None:
    assert cli_mod._upgrade_command("pip")[1:] == ["-m", "pip", "install", "--upgrade", "colabctl"]
    assert cli_mod._upgrade_command("uv") == ["uv", "tool", "upgrade", "colabctl"]


def test_update_check_reports_newer(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "_latest_pypi_version", lambda: "9.9.9")
    result = runner.invoke(cli_mod.app, ["update", "--check"])
    assert result.exit_code == 0
    assert "9.9.9" in result.output
    assert "newer version is available" in result.output


def test_update_reports_up_to_date(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "_latest_pypi_version", lambda: cli_mod.__version__)
    result = runner.invoke(cli_mod.app, ["update", "--check"])
    assert result.exit_code == 0
    assert "up to date" in result.output


def test_update_runs_the_upgrade(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "_latest_pypi_version", lambda: "9.9.9")
    captured: dict[str, list[str]] = {}

    def fake_call(cmd: list[str]) -> int:
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(cli_mod.subprocess, "call", fake_call)
    result = runner.invoke(cli_mod.app, ["update", "--method", "pip"])
    assert result.exit_code == 0
    assert captured["cmd"][1:] == ["-m", "pip", "install", "--upgrade", "colabctl"]


def test_update_pypi_unreachable_errors(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "_latest_pypi_version", lambda: None)
    result = runner.invoke(cli_mod.app, ["update"])
    assert result.exit_code == 1
    assert "PyPI unreachable" in result.output
