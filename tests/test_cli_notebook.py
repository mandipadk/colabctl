"""`colabctl notebook run` — parameterized notebook execution on a remote GPU (1.6.7)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.sdk.client import ColabClient
from conftest import FakeTransport

runner = CliRunner()


def _nb(*cells: str) -> dict:
    return {
        "cells": [{"cell_type": "code", "metadata": {}, "source": c} for c in cells],
        "metadata": {},
        "nbformat": 4,
    }


def test_notebook_run_cell_by_cell_with_executed_artifact(monkeypatch, tmp_path: Path) -> None:
    nb = tmp_path / "n.ipynb"
    nb.write_text(json.dumps(_nb("print(epochs)", "print('done')")))
    out = tmp_path / "out.ipynb"
    monkeypatch.setattr(
        cli_mod, "_make_client", lambda state: ColabClient(transport=FakeTransport())
    )
    result = runner.invoke(
        cli_mod.app,
        ["notebook", "run", str(nb), "--param", "epochs=10", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "ran 3 cell(s)" in result.output  # injected params cell + 2 code cells
    assert out.exists()
    executed = json.loads(out.read_text())
    code = [c for c in executed["cells"] if c["cell_type"] == "code"]
    assert any(c.get("outputs") for c in code)  # outputs were written back


def test_notebook_run_rejects_bad_param(monkeypatch, tmp_path: Path) -> None:
    nb = tmp_path / "n.ipynb"
    nb.write_text(json.dumps(_nb("pass")))
    monkeypatch.setattr(
        cli_mod, "_make_client", lambda state: ColabClient(transport=FakeTransport())
    )
    result = runner.invoke(cli_mod.app, ["notebook", "run", str(nb), "--param", "noequals"])
    assert result.exit_code != 0
