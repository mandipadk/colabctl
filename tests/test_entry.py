"""Friendly missing-extra errors for the console-script launchers (Phase 0.8 sliver).

A bare install must give a clear 'install this extra' message + non-zero exit, not a raw
traceback; an unrelated import failure must NOT be masked as a missing extra.
"""

from __future__ import annotations

import pytest

from colabctl import _entry


def test_require_extra_translates_known_missing_dependency(capsys):
    exc = ModuleNotFoundError("No module named 'typer'", name="typer")
    with pytest.raises(SystemExit) as ei:
        _entry._require_extra("cli", exc, modules=("typer", "rich"))
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert 'pip install "colabctl[cli]"' in err
    assert "typer" in err


def test_require_extra_reraises_unrelated_import_error():
    exc = ModuleNotFoundError("No module named 'numpy'", name="numpy")
    # numpy isn't part of the cli extra → don't mask it as 'install cli'; re-raise as-is.
    with pytest.raises(ModuleNotFoundError, match="numpy"):
        _entry._require_extra("cli", exc, modules=("typer", "rich"))


def test_cli_main_invokes_real_main_when_extra_present(monkeypatch):
    import colabctl.cli as cli

    called: list[bool] = []
    monkeypatch.setattr(cli, "main", lambda: called.append(True))
    _entry.cli_main()  # `from colabctl.cli import main` resolves the patched attribute
    assert called == [True]


def test_mcp_main_invokes_real_main_when_extra_present(monkeypatch):
    import colabctl.mcp_server as mcp

    called: list[bool] = []
    monkeypatch.setattr(mcp, "main", lambda: called.append(True))
    _entry.mcp_main()
    assert called == [True]
