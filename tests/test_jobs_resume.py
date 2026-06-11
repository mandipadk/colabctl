"""Auto-resume of resumable detached jobs on runtime reclamation (Pillar 2).

A scripted transport returns framed poll/launch responses and can inject a reclaim
(``RuntimeUnavailableError``, the native transport's definite "runtime gone" signal).
The backend should re-allocate + relaunch a *resumable* job and surface the error for a
non-resumable one — without running any real subprocess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from colabctl.backends.base import JobSpec, JobState
from colabctl.errors import RuntimeUnavailableError
from colabctl.jobs.backend import DetachedColabBackend
from colabctl.models import ExecutionResult, StreamOutput
from colabctl.state import StateStore
from conftest import FakeTransport

# The job control-frame markers (stable contract with colabctl.jobs.codes).
_BEGIN, _END = "<<<COLABCTL_JOB>>>", "<<<COLABCTL_JOBEND>>>"


def _frame(payload: str) -> ExecutionResult:
    return ExecutionResult(
        status="ok", outputs=[StreamOutput(name="stdout", text=_BEGIN + payload + _END)]
    )


class ScriptedTransport(FakeTransport):
    """Returns canned job frames; can inject one reclaim on the next poll."""

    name = "scripted"

    def __init__(self) -> None:
        super().__init__()
        self.allocate_calls = 0
        self.launch_calls = 0
        self.polls = 0
        self.reclaim_next_poll = False

    async def allocate(self, spec):
        self.allocate_calls += 1
        return await super().allocate(spec)

    async def execute(self, name, code, *, timeout=None, on_output=None) -> ExecutionResult:
        if "start_new_session" in code:  # launch
            self.launch_calls += 1
            return _frame("4242")
        if "status.json" in code:  # poll
            self.polls += 1
            if self.reclaim_next_poll:
                self.reclaim_next_poll = False
                raise RuntimeUnavailableError("simulated reclaim")
            # Report succeeded once the job has been (re)launched at least twice.
            if self.launch_calls >= 2:
                return _frame(json.dumps({"state": "succeeded", "exit_code": 0, "log_size": 0}))
            return _frame(json.dumps({"state": "running", "log_size": 0}))
        return _frame(json.dumps({"offset": 0, "b64": ""}))  # tail


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(home=tmp_path / "home")


def _backend(t: ScriptedTransport, store: StateStore, tmp_path: Path) -> DetachedColabBackend:
    return DetachedColabBackend(t, state=store, root=str(tmp_path / "jobs"), poll_interval=0.01)


async def test_resumable_job_auto_resumes_on_reclaim(store: StateStore, tmp_path: Path) -> None:
    t = ScriptedTransport()
    backend = _backend(t, store, tmp_path)
    info = await backend.submit(JobSpec(code="train()", resumable=True, name="j"))
    assert t.allocate_calls == 1 and t.launch_calls == 1

    t.reclaim_next_poll = True
    result = await backend.status(info.id)  # poll reclaims → resume → re-poll succeeds

    assert t.allocate_calls == 2  # re-allocated a fresh runtime
    assert t.launch_calls == 2  # relaunched the persisted spec
    assert result.state is JobState.SUCCEEDED
    record = store.get_job(info.id)
    assert record is not None and record.incarnations == 2
    assert record.log_offset == 0  # reset for the new runtime's log


async def test_non_resumable_job_surfaces_reclaim(store: StateStore, tmp_path: Path) -> None:
    t = ScriptedTransport()
    backend = _backend(t, store, tmp_path)
    info = await backend.submit(JobSpec(code="train()", resumable=False, name="j"))

    t.reclaim_next_poll = True
    with pytest.raises(RuntimeUnavailableError):
        await backend.status(info.id)
    assert t.allocate_calls == 1  # did NOT re-allocate
    assert t.launch_calls == 1
