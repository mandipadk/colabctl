"""Tests for the HF Jobs backend: flavor/state mapping + orchestration vs a fake SDK."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from colabctl.backends.base import JobSpec, JobState
from colabctl.backends.hf_backend import HFJobsBackend, hf_flavor, hf_state
from colabctl.errors import ConfigurationError
from colabctl.models import Accelerator


def test_hf_flavor_mapping():
    assert hf_flavor(Accelerator.T4) == "t4-small"
    assert hf_flavor(Accelerator.A100) == "a100-large"
    assert hf_flavor(Accelerator.H100) == "h100x1"
    assert hf_flavor(Accelerator.NONE) == "cpu-basic"
    with pytest.raises(ConfigurationError):
        hf_flavor(Accelerator.G4)


def test_hf_state_mapping():
    assert hf_state("COMPLETED") is JobState.SUCCEEDED
    assert hf_state("ERROR") is JobState.FAILED
    assert hf_state("CANCELED") is JobState.CANCELLED
    assert hf_state("RUNNING") is JobState.RUNNING
    assert hf_state("QUEUED") is JobState.PENDING


def make_fake_hf(stage="COMPLETED", logs=("hello\n",)):
    captured: dict = {}
    cancelled: list[str] = []

    def run_job(*, image, command, flavor, env=None, token=None):
        captured.update(image=image, command=command, flavor=flavor, env=env, token=token)
        return SimpleNamespace(id="hfjob-1", url="https://huggingface.co/jobs/x")

    def inspect_job(*, job_id):
        return SimpleNamespace(status=SimpleNamespace(stage=stage))

    def fetch_job_logs(*, job_id):
        return list(logs)

    def cancel_job(*, job_id):
        cancelled.append(job_id)

    hf = SimpleNamespace(
        run_job=run_job,
        inspect_job=inspect_job,
        fetch_job_logs=fetch_job_logs,
        cancel_job=cancel_job,
    )
    return hf, captured, cancelled


async def test_hf_run_success(monkeypatch):
    hf, captured, _ = make_fake_hf(stage="COMPLETED", logs=("hello from hf\n",))
    monkeypatch.setattr("colabctl.backends.hf_backend._load_hf", lambda: hf)
    backend = HFJobsBackend()
    result = await backend.run(
        JobSpec(code="print('hi')", accelerator=Accelerator.A100, requirements=["torch"])
    )
    assert result.ok
    assert result.state is JobState.SUCCEEDED
    assert result.stdout == "hello from hf\n"
    assert captured["flavor"] == "a100-large"
    assert captured["command"][0] == "bash"
    assert "pip install" in captured["command"][2]
    assert "python -c" in captured["command"][2]


async def test_hf_failed_job(monkeypatch):
    hf, _, _ = make_fake_hf(stage="ERROR")
    monkeypatch.setattr("colabctl.backends.hf_backend._load_hf", lambda: hf)
    backend = HFJobsBackend()
    result = await backend.run(JobSpec(code="raise SystemExit(1)", accelerator=Accelerator.T4))
    assert not result.ok
    assert result.state is JobState.FAILED


async def test_hf_cpu_uses_cpu_image(monkeypatch):
    hf, captured, _ = make_fake_hf()
    monkeypatch.setattr("colabctl.backends.hf_backend._load_hf", lambda: hf)
    backend = HFJobsBackend()
    await backend.run(JobSpec(code="x=1", accelerator=Accelerator.NONE))
    assert captured["flavor"] == "cpu-basic"
    assert captured["image"] == "python:3.12"


async def test_hf_cancel(monkeypatch):
    hf, _, cancelled = make_fake_hf()
    monkeypatch.setattr("colabctl.backends.hf_backend._load_hf", lambda: hf)
    backend = HFJobsBackend()
    info = await backend.submit(JobSpec(code="x=1", accelerator=Accelerator.T4))
    await backend.cancel(info.id)
    assert cancelled == [info.id]
