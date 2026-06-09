"""Tests for the MCP batch-job tools (JobTools) with a fake backend factory."""

from __future__ import annotations

from colabctl.backends.base import JobResult, JobState
from colabctl.mcp_server import JobTools
from conftest import FakeBackend


async def test_run_job_returns_result_dict():
    fb = FakeBackend(
        name="modal",
        result=JobResult(id="j", backend="modal", state=JobState.SUCCEEDED, stdout="ok\n"),
    )
    tools = JobTools(backend_factory=lambda name: fb)
    out = await tools.run_job("modal", "print(1)", gpu="T4", requirements=["torch"])
    assert out["ok"] is True
    assert out["state"] == "SUCCEEDED"
    assert "ok" in out["stdout"]
    assert fb.specs[0].accelerator.value == "T4"
    assert fb.specs[0].requirements == ["torch"]


async def test_run_job_cpu():
    fb = FakeBackend()
    tools = JobTools(backend_factory=lambda name: fb)
    await tools.run_job("modal", "x=1", gpu="none")
    assert fb.specs[0].accelerator.value == "NONE"


async def test_list_backends():
    tools = JobTools(backend_factory=lambda name: FakeBackend(name=name, accels=["T4", "A100"]))
    out = await tools.list_backends()
    assert {b["name"] for b in out} == {"colab", "modal", "vertex", "hf", "kaggle"}
    assert out[0]["accelerators"] == ["A100", "T4"]


async def test_backend_instances_cached():
    built: list[str] = []

    def factory(name):
        built.append(name)
        return FakeBackend(name=name)

    tools = JobTools(backend_factory=factory)
    await tools.run_job("modal", "x=1")
    await tools.run_job("modal", "y=2")
    assert built == ["modal"]  # built once, reused
