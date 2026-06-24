"""Tests for the papermill-style notebook adapter."""

from __future__ import annotations

import json

from colabctl.backends.base import JobState
from colabctl.models import Accelerator, ErrorOutput, ExecutionResult
from colabctl.notebook import (
    code_cells,
    inject_parameters,
    notebook_to_script,
    run_notebook,
    run_notebook_job,
)
from colabctl.sdk import ColabClient
from conftest import FakeBackend, FakeTransport


def _nb(*cells, params_cell=None):
    out = []
    if params_cell is not None:
        out.append(
            {"cell_type": "code", "metadata": {"tags": ["parameters"]}, "source": params_cell}
        )
    for c in cells:
        out.append({"cell_type": "code", "metadata": {}, "source": c})
    return {"cells": out, "metadata": {}, "nbformat": 4}


def test_code_cells_joins_source_and_skips_markdown():
    nb = {
        "cells": [
            {"cell_type": "markdown", "source": ["# title"]},
            {"cell_type": "code", "source": ["import os\n", "print(os.getcwd())"]},
            {"cell_type": "code", "source": "x = 1"},
            {"cell_type": "code", "source": ["   "]},  # empty → skipped
        ]
    }
    assert code_cells(nb) == ["import os\nprint(os.getcwd())", "x = 1"]


def test_inject_parameters_after_tagged_cell():
    nb = _nb("print(epochs)", params_cell=["epochs = 1\n"])
    injected = inject_parameters(nb, {"epochs": 10})
    # the injected cell is right after the tagged params cell
    assert injected["cells"][1]["metadata"]["tags"] == ["injected-parameters"]
    assert injected["cells"][1]["source"] == ["epochs = 10\n"]


def test_inject_parameters_at_top_when_untagged():
    nb = _nb("print(epochs)")
    injected = inject_parameters(nb, {"epochs": 3, "name": "run"})
    assert injected["cells"][0]["metadata"]["tags"] == ["injected-parameters"]
    assert injected["cells"][0]["source"] == ["epochs = 3\n", "name = 'run'\n"]


def test_notebook_to_script():
    nb = _nb("a = 1", "b = 2")
    script = notebook_to_script(nb, {"a": 5})
    assert "a = 5" in script  # injected param
    assert "b = 2" in script
    assert script.index("a = 5") < script.index("b = 2")  # params first


async def test_run_notebook_cell_by_cell(tmp_path):
    nb_path = tmp_path / "n.ipynb"
    nb_path.write_text(json.dumps(_nb("step1()", "step2()")))
    session = ColabClient(transport=FakeTransport()).attach("j")
    results = await run_notebook(session, nb_path, parameters={"x": 1})
    assert len(results) == 3  # injected params cell + step1 + step2
    assert all(r.ok for r in results)


class FailingSecondCellTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self._n = 0

    async def execute(self, name, code, *, timeout=None, on_output=None):
        self._n += 1
        if self._n == 2:
            return ExecutionResult(status="error", outputs=[ErrorOutput(ename="E", evalue="x")])
        return await super().execute(name, code, timeout=timeout, on_output=on_output)


async def test_run_notebook_stops_on_error(tmp_path):
    nb_path = tmp_path / "n.ipynb"
    nb_path.write_text(json.dumps(_nb("ok()", "boom()", "never()")))
    session = ColabClient(transport=FailingSecondCellTransport()).attach("j")
    results = await run_notebook(session, nb_path)
    assert len(results) == 2  # stopped after the failing 2nd cell
    assert not results[1].ok


async def test_run_notebook_job(tmp_path):
    nb_path = tmp_path / "n.ipynb"
    nb_path.write_text(json.dumps(_nb("train()")))
    backend = FakeBackend()
    result = await run_notebook_job(backend, nb_path, accelerator=Accelerator.A100)
    assert result.state is JobState.SUCCEEDED
    assert backend.specs[0].accelerator is Accelerator.A100
    assert "train()" in backend.specs[0].code


async def test_executed_notebook_fills_cell_outputs(tmp_path):
    from colabctl.notebook import executed_notebook, load_notebook

    nb_path = tmp_path / "n.ipynb"
    nb_path.write_text(json.dumps(_nb("print(1)", "print(2)")))
    session = ColabClient(transport=FakeTransport()).attach("j")
    results = await run_notebook(session, nb_path, parameters={"x": 1})

    out = executed_notebook(load_notebook(nb_path), results, parameters={"x": 1})
    code = [c for c in out["cells"] if c["cell_type"] == "code"]
    assert len(code) == 3  # injected params cell + 2 code cells
    assert all(c.get("execution_count") for c in code)  # each got an execution count
    assert code[-1]["outputs"][0]["output_type"] == "stream"
    assert code[-1]["outputs"][0]["text"]  # the cell's captured stdout
