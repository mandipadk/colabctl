"""Tests for the Colab batch backend over the FakeTransport."""

from __future__ import annotations

from colabctl.backends.base import JobSpec, JobState
from colabctl.backends.colab import ColabBackend
from colabctl.models import Accelerator, ErrorOutput, ExecutionResult
from conftest import FakeTransport


class FailingTransport(FakeTransport):
    async def execute(self, name, code, *, timeout=None, on_output=None):
        self.executed.append((name, code))
        return ExecutionResult(
            status="error",
            outputs=[ErrorOutput(ename="ValueError", evalue="boom", traceback=["t"])],
        )


async def test_colab_backend_success_runs_and_stops():
    t = FakeTransport()
    backend = ColabBackend(t)
    result = await backend.run(JobSpec(code="print(1)", accelerator=Accelerator.T4, name="j"))
    assert result.ok
    assert result.state is JobState.SUCCEEDED
    assert "ran:" in result.stdout
    assert t.stopped == ["j"]  # runtime released after the job


async def test_colab_backend_code_failure_is_failed_not_raised():
    backend = ColabBackend(FailingTransport())
    result = await backend.run(JobSpec(code="boom()", name="j"))
    assert not result.ok
    assert result.state is JobState.FAILED
    assert "ValueError: boom" in (result.error or "")


async def test_colab_backend_installs_requirements_first():
    t = FakeTransport()
    backend = ColabBackend(t)
    await backend.run(JobSpec(code="import torch", requirements=["torch"], name="j"))
    # Two executes: the pip install, then the user code.
    assert len(t.executed) == 2
    assert "pip" in t.executed[0][1] and "install" in t.executed[0][1]
    assert t.executed[1][1] == "import torch"


async def test_colab_backend_cancel_before_run():
    t = FakeTransport()
    backend = ColabBackend(t)
    info = await backend.submit(JobSpec(code="print(1)", name="j"))
    await backend.cancel(info.id)
    status = await backend.status(info.id)
    assert status.state is JobState.CANCELLED


async def test_colab_backend_logs():
    t = FakeTransport()
    backend = ColabBackend(t)
    info = await backend.submit(JobSpec(code="print('hello')", name="j"))
    await backend.result(info.id)
    assert "ran:" in await backend.logs(info.id)
