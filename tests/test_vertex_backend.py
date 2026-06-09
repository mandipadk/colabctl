"""Tests for the Vertex backend: accelerator/state mapping + orchestration vs a fake SDK."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from colabctl.backends.base import JobSpec, JobState
from colabctl.backends.vertex_backend import VertexBackend, vertex_accelerator, vertex_state
from colabctl.errors import ConfigurationError
from colabctl.models import Accelerator


def test_vertex_accelerator_mapping():
    assert vertex_accelerator(Accelerator.T4) == ("NVIDIA_TESLA_T4", "n1-standard-4")
    assert vertex_accelerator(Accelerator.A100) == ("NVIDIA_TESLA_A100", "a2-highgpu-1g")
    assert vertex_accelerator(Accelerator.NONE) == (None, "n1-standard-4")
    with pytest.raises(ConfigurationError):
        vertex_accelerator(Accelerator.V6E1)


def test_vertex_state_mapping():
    assert vertex_state("JOB_STATE_SUCCEEDED") is JobState.SUCCEEDED
    assert vertex_state("JOB_STATE_FAILED") is JobState.FAILED
    assert vertex_state("JOB_STATE_CANCELLING") is JobState.CANCELLED
    assert vertex_state("JOB_STATE_RUNNING") is JobState.RUNNING
    assert vertex_state("JOB_STATE_QUEUED") is JobState.PENDING
    assert vertex_state("JOB_STATE_WHATEVER") is JobState.UNKNOWN


def make_fake_aiplatform():
    inits: list[dict] = []

    class _State:
        def __init__(self, name):
            self.name = name

        def __str__(self):
            return self.name

    class FakeCustomJob:
        states: dict[str, str] = {}
        last_kwargs: dict | None = None

        def __init__(self):
            self.resource_name = None

        @classmethod
        def from_local_script(cls, **kwargs):
            cls.last_kwargs = kwargs
            return cls()

        def submit(self):
            self.resource_name = "projects/p/locations/us-central1/customJobs/123"
            FakeCustomJob.states[self.resource_name] = "JOB_STATE_RUNNING"

        @classmethod
        def get(cls, resource_name):
            job = cls()
            job.resource_name = resource_name
            job.state = _State(cls.states.get(resource_name, "JOB_STATE_PENDING"))
            return job

        def cancel(self):
            FakeCustomJob.states[self.resource_name] = "JOB_STATE_CANCELLED"

    ns = SimpleNamespace(init=lambda **kw: inits.append(kw), CustomJob=FakeCustomJob)
    return ns, inits, FakeCustomJob


async def test_vertex_submit_initializes_and_maps_accelerator(monkeypatch):
    fake, inits, JobClass = make_fake_aiplatform()
    monkeypatch.setattr("colabctl.backends.vertex_backend._load_aiplatform", lambda: fake)
    backend = VertexBackend(project="p", staging_bucket="gs://b")
    info = await backend.submit(JobSpec(code="print(1)", accelerator=Accelerator.T4, name="job"))
    assert info.state is JobState.PENDING
    assert info.detail  # resource_name set
    assert inits[0]["project"] == "p" and inits[0]["staging_bucket"] == "gs://b"
    assert JobClass.last_kwargs["accelerator_type"] == "NVIDIA_TESLA_T4"
    assert JobClass.last_kwargs["machine_type"] == "n1-standard-4"
    assert JobClass.last_kwargs["accelerator_count"] == 1


async def test_vertex_status_reflects_state(monkeypatch):
    fake, _, _ = make_fake_aiplatform()
    monkeypatch.setattr("colabctl.backends.vertex_backend._load_aiplatform", lambda: fake)
    backend = VertexBackend(project="p", staging_bucket="gs://b")
    info = await backend.submit(JobSpec(code="print(1)"))
    status = await backend.status(info.id)
    assert status.state is JobState.RUNNING  # submit set RUNNING


async def test_vertex_result_waits_for_terminal(monkeypatch):
    fake, _, JobClass = make_fake_aiplatform()
    monkeypatch.setattr("colabctl.backends.vertex_backend._load_aiplatform", lambda: fake)
    backend = VertexBackend(project="p", staging_bucket="gs://b", poll_interval=0.001)
    info = await backend.submit(JobSpec(code="print(1)"))
    JobClass.states[info.detail] = "JOB_STATE_SUCCEEDED"
    result = await backend.result(info.id)
    assert result.ok
    assert result.state is JobState.SUCCEEDED


async def test_vertex_cpu_omits_accelerator(monkeypatch):
    fake, _, JobClass = make_fake_aiplatform()
    monkeypatch.setattr("colabctl.backends.vertex_backend._load_aiplatform", lambda: fake)
    backend = VertexBackend(project="p", staging_bucket="gs://b")
    await backend.submit(JobSpec(code="x=1", accelerator=Accelerator.NONE))
    assert "accelerator_type" not in JobClass.last_kwargs


async def test_vertex_requires_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    backend = VertexBackend(project=None, staging_bucket="gs://b")
    with pytest.raises(ConfigurationError):
        await backend.submit(JobSpec(code="x=1"))
