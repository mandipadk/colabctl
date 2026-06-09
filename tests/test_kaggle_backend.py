"""Tests for the Kaggle backend: accelerator/state mapping + orchestration vs a fake API."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from colabctl.backends.base import JobSpec, JobState
from colabctl.backends.kaggle_backend import KaggleBackend, kaggle_accelerator, kaggle_state
from colabctl.errors import ColabctlError, ConfigurationError
from colabctl.models import Accelerator


def test_kaggle_accelerator_mapping():
    assert kaggle_accelerator(Accelerator.T4) == (True, "NvidiaTeslaT4")
    assert kaggle_accelerator(Accelerator.NONE) == (False, None)
    with pytest.raises(ConfigurationError):
        kaggle_accelerator(Accelerator.A100)  # Kaggle has no A100


def test_kaggle_state_mapping():
    assert kaggle_state("complete") is JobState.SUCCEEDED
    assert kaggle_state("error") is JobState.FAILED
    assert kaggle_state("cancelAcknowledged") is JobState.CANCELLED
    assert kaggle_state("running") is JobState.RUNNING
    assert kaggle_state("queued") is JobState.PENDING


class FakeKaggleApi:
    def __init__(self, status="complete", log="kaggle log\n"):
        self.pushed: dict | None = None
        self._status = status
        self._log = log

    def kernels_push(self, folder):
        self.pushed = {
            "metadata": json.loads((Path(folder) / "kernel-metadata.json").read_text()),
            "script": (Path(folder) / "script.py").read_text(),
        }

    def kernels_status(self, kernel_id):
        return SimpleNamespace(status=self._status)

    def kernels_output(self, kernel_id, path):
        (Path(path) / "out.log").write_text(self._log)


async def test_kaggle_run_success(monkeypatch):
    api = FakeKaggleApi(status="complete", log="hi from kaggle\n")
    monkeypatch.setattr("colabctl.backends.kaggle_backend._load_kaggle", lambda: api)
    backend = KaggleBackend(username="me")
    result = await backend.run(
        JobSpec(code="print('hi')", accelerator=Accelerator.T4, requirements=["torch"])
    )
    assert result.ok
    assert result.state is JobState.SUCCEEDED
    assert "hi from kaggle" in result.stdout
    md = api.pushed["metadata"]
    assert md["id"].startswith("me/")
    assert md["enable_gpu"] == "true"
    assert md["accelerator"] == "NvidiaTeslaT4"
    script = api.pushed["script"]
    assert "pip" in script and "install" in script  # requirements installed in-script


async def test_kaggle_cpu_disables_gpu(monkeypatch):
    api = FakeKaggleApi()
    monkeypatch.setattr("colabctl.backends.kaggle_backend._load_kaggle", lambda: api)
    backend = KaggleBackend(username="me")
    await backend.run(JobSpec(code="x=1", accelerator=Accelerator.NONE))
    assert api.pushed["metadata"]["enable_gpu"] == "false"
    assert "accelerator" not in api.pushed["metadata"]


async def test_kaggle_requires_username(monkeypatch):
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    backend = KaggleBackend(username=None)
    with pytest.raises(ConfigurationError):
        await backend.submit(JobSpec(code="x=1", accelerator=Accelerator.T4))


async def test_kaggle_cancel_not_supported():
    backend = KaggleBackend(username="me")
    with pytest.raises(ColabctlError):
        await backend.cancel("me/slug")
