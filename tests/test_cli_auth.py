"""CLI `colabctl auth` commands: scopes, status (diagnosis), login (gcloud wrapper)."""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.auth.diagnostics import COLABORATORY_SCOPE, DRIVE_FILE_SCOPE

runner = CliRunner()


class _Provider:
    def __init__(self, quota: str | None = None) -> None:
        self._quota = quota

    async def token(self) -> str:
        return "tok"

    @property
    def quota_project_id(self) -> str | None:
        return self._quota


def _patch_status(monkeypatch, *, scopes: list[str], quota: str | None) -> None:
    monkeypatch.setattr(cli_mod, "_adc_provider", lambda: _Provider(quota))

    async def fake_info(token: str, http: Any = None) -> dict[str, Any]:
        return {"email": "me@example.com", "scope": " ".join(scopes)}

    monkeypatch.setattr(cli_mod, "token_info", fake_info)


def test_auth_scopes_prints_login_command() -> None:
    result = runner.invoke(cli_mod.app, ["auth", "scopes"])
    assert result.exit_code == 0
    assert "application-default login" in result.output
    assert "colaboratory" in result.output and "drive.file" in result.output


def test_auth_status_all_good(monkeypatch) -> None:
    _patch_status(monkeypatch, scopes=[COLABORATORY_SCOPE, DRIVE_FILE_SCOPE], quota="proj-1")
    result = runner.invoke(cli_mod.app, ["auth", "status"])
    assert result.exit_code == 0
    assert "me@example.com" in result.output
    assert "colaboratory:  yes" in result.output
    assert "drive.file:    yes" in result.output
    assert "proj-1" in result.output
    assert "fix" not in result.output  # nothing to fix → no hints


def test_auth_status_flags_missing_scope_and_quota(monkeypatch) -> None:
    _patch_status(monkeypatch, scopes=[COLABORATORY_SCOPE], quota=None)  # drive missing; no quota
    result = runner.invoke(cli_mod.app, ["auth", "status"])
    assert result.exit_code == 0
    assert "drive.file:    NO" in result.output
    assert "NOT SET" in result.output
    assert "auth login" in result.output  # scope-fix hint
    assert "set-quota-project" in result.output  # quota-fix hint


def test_auth_login_without_gcloud_guides_user(monkeypatch) -> None:
    def boom(_cmd) -> int:
        raise FileNotFoundError

    monkeypatch.setattr(cli_mod.subprocess, "call", boom)
    result = runner.invoke(cli_mod.app, ["auth", "login"])
    assert result.exit_code == 1
    assert "gcloud not found" in result.output
