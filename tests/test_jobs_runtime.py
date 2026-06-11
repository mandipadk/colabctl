"""End-to-end KernelJobRuntime over a transport that really runs the emitted code.

``LocalExecTransport.execute`` runs each generated payload as a local subprocess, so a
full detached-job lifecycle (launch → poll → tail → cancel) is exercised for real,
hermetically — the same code paths the native transport will run on Colab, minus the
network. Slower than a pure unit test (real processes, real timing) but worth it: this
is the one place the whole substrate is proven to actually work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from colabctl.backends.base import JobState
from colabctl.errors import JobError
from colabctl.jobs.runtime import KernelJobRuntime, job_state_from
from colabctl.models import ExecutionResult, StreamOutput
from conftest import FakeTransport, LocalExecTransport


async def _wait_until(rt: KernelJobRuntime, job_id: str, predicate, *, tries=200, delay=0.05):
    snap: dict[str, object] = {}
    for _ in range(tries):
        snap = await rt.poll("sess", job_id)
        if predicate(job_state_from(snap)):
            return snap
        await asyncio.sleep(delay)
    raise AssertionError(f"job {job_id} never satisfied predicate; last={snap}")


@pytest.fixture
def runtime(tmp_path: Path) -> KernelJobRuntime:
    return KernelJobRuntime(LocalExecTransport(), root=str(tmp_path / "jobs"))


async def test_full_lifecycle_launch_poll_tail(runtime: KernelJobRuntime) -> None:
    script = (
        "import time\n"
        "for i in range(3):\n    print('line', i)\n    time.sleep(0.02)\n"
        "print('DONE')\n"
    )
    launched = await runtime.launch("sess", "job1", script=script)
    assert launched.pid > 0
    assert launched.remote_dir.endswith("/job1")

    snap = await _wait_until(runtime, "job1", lambda s: s.is_terminal)
    assert job_state_from(snap) is JobState.SUCCEEDED
    assert snap.get("exit_code") == 0

    data, offset = await runtime.tail("sess", "job1")
    text = data.decode()
    assert "line 0" in text and "line 2" in text and "DONE" in text
    assert offset == len(data)


async def test_tail_resumes_from_offset(runtime: KernelJobRuntime) -> None:
    await runtime.launch("sess", "job1", script="print('AAAA')\nprint('BBBB')\n")
    await _wait_until(runtime, "job1", lambda s: s.is_terminal)

    first, off1 = await runtime.tail("sess", "job1", max_bytes=5)  # partial read
    assert len(first) == 5
    rest, off2 = await runtime.tail("sess", "job1", offset=off1)  # resume exactly
    assert off2 >= off1
    assert (first + rest).decode().startswith("AAAA")
    # No overlap and no gap: the two reads concatenate to the whole log.
    full, _ = await runtime.tail("sess", "job1")
    assert first + rest == full


async def test_cancel_stops_running_job(runtime: KernelJobRuntime) -> None:
    await runtime.launch("sess", "job2", script="import time\ntime.sleep(60)\nprint('NOPE')\n")
    await _wait_until(runtime, "job2", lambda s: s is JobState.RUNNING)

    cancelled = await runtime.cancel("sess", "job2")
    assert cancelled is True
    snap = await runtime.poll("sess", "job2")
    assert job_state_from(snap) is JobState.CANCELLED


async def test_poll_missing_job_is_pending_state(runtime: KernelJobRuntime) -> None:
    # No launch — the directory does not exist yet.
    snap = await runtime.poll("sess", "ghost")
    assert job_state_from(snap) is JobState.PENDING


async def test_launch_failure_raises_job_error(tmp_path: Path) -> None:
    class BrokenTransport(FakeTransport):
        async def execute(self, name, code, *, timeout=None, on_output=None):
            return ExecutionResult(
                status="error", outputs=[StreamOutput(name="stderr", text="boom")]
            )

    rt = KernelJobRuntime(BrokenTransport(), root=str(tmp_path / "jobs"))
    with pytest.raises(JobError):
        await rt.launch("sess", "job1", script="print(1)")
