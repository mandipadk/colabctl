"""The `cli` transport resolves the `colab` binary (PATH → colabctl's own venv) and, when it's
genuinely missing, raises an actionable error pointing at the fix + binary-free transports."""

from __future__ import annotations

from pathlib import Path

import pytest

from colabctl.errors import CLIError
from colabctl.transport.cli import ColabCliTransport

_ADAPTER = "colabctl.transport.cli.adapter"


def test_resolve_bin_prefers_path(monkeypatch) -> None:
    monkeypatch.setattr(f"{_ADAPTER}.shutil.which", lambda _b: "/usr/local/bin/colab")
    assert ColabCliTransport()._resolve_bin() == "/usr/local/bin/colab"


def test_resolve_bin_falls_back_to_colabctl_venv(monkeypatch, tmp_path: Path) -> None:
    # Not on PATH, but co-installed next to colabctl's interpreter (the uv-tool case).
    monkeypatch.setattr(f"{_ADAPTER}.shutil.which", lambda _b: None)
    venv_bin = tmp_path / "bin"
    venv_bin.mkdir()
    (venv_bin / "colab").write_text("#!/bin/sh\n")
    monkeypatch.setattr(f"{_ADAPTER}.sys.executable", str(venv_bin / "python"))
    assert ColabCliTransport()._resolve_bin() == str(venv_bin / "colab")


def test_resolve_bin_missing_returns_bare_name(monkeypatch) -> None:
    monkeypatch.setattr(f"{_ADAPTER}.shutil.which", lambda _b: None)
    monkeypatch.setattr(f"{_ADAPTER}.sys.executable", "/no/such/python")
    assert ColabCliTransport()._resolve_bin() == "colab"  # → subprocess raises → friendly error


async def test_missing_binary_error_is_actionable() -> None:
    # An absolute, nonexistent bin can't be found on PATH or next to sys.executable, so the
    # subprocess genuinely raises FileNotFoundError → the friendly CLIError (hermetic even if
    # a real `colab` happens to be on this machine's PATH).
    t = ColabCliTransport(colab_bin="/nonexistent/colab-xyz-123")
    with pytest.raises(CLIError) as ei:
        await t.list_sessions()
    msg = str(ei.value)
    assert "google-colab-cli" in msg  # names the real missing package
    assert "--with google-colab-cli" in msg  # the co-install fix
    assert "-t browser" in msg and "-t native" in msg  # binary-free alternatives
