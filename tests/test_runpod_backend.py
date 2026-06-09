"""Tests for the RunPod backend: gpu/state mapping + orchestration vs a fake SDK."""

from __future__ import annotations

import pytest

from colabctl.backends.base import JobSpec, JobState
from colabctl.backends.runpod_backend import RunPodBackend, runpod_gpu, runpod_state
from colabctl.errors import ConfigurationError
from colabctl.models import Accelerator


def test_runpod_gpu_mapping():
    assert runpod_gpu(Accelerator.A100) == "NVIDIA A100 80GB PCIe"
    assert runpod_gpu(Accelerator.T4) == "NVIDIA T4"
    with pytest.raises(ConfigurationError):
        runpod_gpu(Accelerator.NONE)  # GPU-only
    with pytest.raises(ConfigurationError):
        runpod_gpu(Accelerator.G4)


def test_runpod_state_mapping():
    assert runpod_state("RUNNING") is JobState.RUNNING
    assert runpod_state("EXITED") is JobState.SUCCEEDED
    assert runpod_state("TERMINATED") is JobState.CANCELLED
    assert runpod_state("STOPPED") is JobState.SUCCEEDED
    assert runpod_state("") is JobState.PENDING


class FakeRunpod:
    def __init__(self, status="EXITED"):
        self.api_key = None
        self.created: dict | None = None
        self.terminated: list[str] = []
        self._status = status

    def create_pod(self, **kwargs):
        self.created = kwargs
        return {"id": "pod-1"}

    def get_pod(self, pod_id):
        return {"id": pod_id, "desiredStatus": self._status}

    def terminate_pod(self, pod_id):
        self.terminated.append(pod_id)


async def test_runpod_run_success(monkeypatch):
    fake = FakeRunpod(status="EXITED")
    monkeypatch.setattr("colabctl.backends.runpod_backend._load_runpod", lambda api_key: fake)
    backend = RunPodBackend()
    result = await backend.run(
        JobSpec(code="print('hi')", accelerator=Accelerator.A100, requirements=["torch"])
    )
    assert result.ok
    assert result.state is JobState.SUCCEEDED
    assert fake.created["gpu_type_id"] == "NVIDIA A100 80GB PCIe"
    assert "python -c" in fake.created["docker_args"]
    assert "pip install" in fake.created["docker_args"]
    assert fake.terminated == ["pod-1"]  # always terminated on result


async def test_runpod_is_gpu_only():
    backend = RunPodBackend()
    with pytest.raises(ConfigurationError):
        await backend.submit(JobSpec(code="x=1", accelerator=Accelerator.NONE))


async def test_runpod_cancel_terminates(monkeypatch):
    fake = FakeRunpod(status="RUNNING")
    monkeypatch.setattr("colabctl.backends.runpod_backend._load_runpod", lambda api_key: fake)
    backend = RunPodBackend()
    info = await backend.submit(JobSpec(code="x=1", accelerator=Accelerator.T4))
    await backend.cancel(info.id)
    assert fake.terminated == [info.id]
