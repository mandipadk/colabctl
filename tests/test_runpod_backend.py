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


# --- spot / interruptible (Phase 2c) ----------------------------------------


class FakeGraphQL:
    def __init__(self, pod_id: str | None = "spot-1"):
        self.calls: list[tuple[str, dict]] = []
        self._pod_id = pod_id

    async def __call__(self, query: str, variables: dict) -> dict:
        self.calls.append((query, variables))
        if self._pod_id is None:
            return {"podRentInterruptable": None}  # bid didn't clear
        return {"podRentInterruptable": {"id": self._pod_id, "desiredStatus": "RUNNING"}}


def test_runpod_capabilities_advertise_spot():
    caps = RunPodBackend().capabilities
    assert caps.supports_spot and caps.prepaid_wallet
    assert caps.preempt_notice_seconds == 120


async def test_runpod_spot_bids_via_graphql():
    gql = FakeGraphQL()
    backend = RunPodBackend(graphql=gql)
    info = await backend.submit(
        JobSpec(code="train()", accelerator=Accelerator.A100, spot=True, max_price_usd_hr=1.5)
    )
    assert info.state is JobState.RUNNING and "spot bid" in (info.detail or "")
    _q, variables = gql.calls[0]
    inp = variables["input"]
    assert inp["bidPerGpu"] == 1.5  # the per-job cap becomes the bid
    assert inp["gpuTypeId"] == "NVIDIA A100 80GB PCIe" and inp["cloudType"] == "COMMUNITY"
    assert "python -c" in inp["dockerArgs"]


async def test_runpod_spot_requires_a_bid():
    backend = RunPodBackend(graphql=FakeGraphQL())
    with pytest.raises(ConfigurationError, match="max bid"):
        await backend.submit(JobSpec(code="x=1", accelerator=Accelerator.A100, spot=True))


async def test_runpod_spot_uncleared_bid_is_infra_error():
    from colabctl.errors import ColabctlError

    backend = RunPodBackend(graphql=FakeGraphQL(pod_id=None))
    with pytest.raises(ColabctlError, match="did not clear"):
        await backend.submit(
            JobSpec(code="x=1", accelerator=Accelerator.A100, spot=True, max_price_usd_hr=1.0)
        )
