"""Process liveness (Phase 1.6.3): a runner killed without writing exit_code -> FAILED.

Before this, a status.json stuck at 'running' (OOM/SIGKILL'd runner) made the job lie
RUNNING forever. The poll snapshot now carries runner_alive, and a 'running' snapshot whose
runner is gone resolves to FAILED with an honest event.
"""

from __future__ import annotations

import json
from pathlib import Path

from colabctl.backends.base import JobSpec, JobState
from colabctl.jobs.backend import DetachedColabBackend
from colabctl.jobs.runtime import job_state_from
from colabctl.models import ExecutionResult, StreamOutput
from colabctl.state import StateStore
from conftest import FakeTransport

_BEGIN, _END = "<<<COLABCTL_JOB>>>", "<<<COLABCTL_JOBEND>>>"


def _frame(payload: str) -> ExecutionResult:
    return ExecutionResult(
        status="ok", outputs=[StreamOutput(name="stdout", text=_BEGIN + payload + _END)]
    )


def test_job_state_from_dead_runner_is_failed() -> None:
    assert job_state_from({"state": "running", "runner_alive": False}) is JobState.FAILED
    assert job_state_from({"state": "running", "runner_alive": True}) is JobState.RUNNING
    # Unknown liveness (older runner / missing field) -> trust the state string.
    assert job_state_from({"state": "running"}) is JobState.RUNNING


class _DeadRunnerTransport(FakeTransport):
    """Launch succeeds, but every poll reports a 'running' status with a dead runner."""

    name = "scripted"

    async def execute(self, name, code, *, timeout=None, on_output=None) -> ExecutionResult:
        if "start_new_session" in code:  # launch
            return _frame("99")
        if "status.json" in code:  # poll: still 'running' but the runner pid is gone
            return _frame(json.dumps({"state": "running", "runner_alive": False, "log_size": 0}))
        return _frame(json.dumps({"offset": 0, "b64": ""}))  # tail


async def test_status_resolves_dead_runner_to_failed(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    backend = DetachedColabBackend(
        _DeadRunnerTransport(), state=store, root=str(tmp_path / "jobs"), poll_interval=0.01
    )
    info = await backend.submit(JobSpec(code="x", name="j"))

    out = await backend.status(info.id)

    assert out.state is JobState.FAILED  # not a permanent RUNNING lie
    rec = store.get_job(info.id)
    assert rec is not None
    assert any(
        e.to_state is JobState.FAILED and e.reason == "runner process died" for e in rec.events
    )
