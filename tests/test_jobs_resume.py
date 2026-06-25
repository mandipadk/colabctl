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

from colabctl.allocation import AllocationGate
from colabctl.backends.base import JobSpec, JobState
from colabctl.errors import AllocationError, RuntimeUnavailableError
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


def _backend(
    t: ScriptedTransport, store: StateStore, tmp_path: Path, *, max_incarnations: int = 3
) -> DetachedColabBackend:
    # A zero-backoff gate keeps the bound's logic while running the test instantly.
    return DetachedColabBackend(
        t,
        state=store,
        root=str(tmp_path / "jobs"),
        poll_interval=0.01,
        max_incarnations=max_incarnations,
        gate=AllocationGate(backoff_base=0.0),
    )


class FlappingTransport(ScriptedTransport):
    """Reclaims on every wait-cycle's *first* poll, then 'recovers' on the re-poll — the
    pattern that makes naive auto-resume re-allocate a paid GPU every cycle, forever."""

    def __init__(self) -> None:
        super().__init__()
        self._poll_seq = 0

    async def execute(self, name, code, *, timeout=None, on_output=None) -> ExecutionResult:
        if "start_new_session" in code:
            self.launch_calls += 1
            return _frame("4242")
        if "status.json" in code:
            self.polls += 1
            self._poll_seq += 1
            if self._poll_seq % 2 == 1:  # first poll of each cycle: runtime gone
                raise RuntimeUnavailableError("flap")
            return _frame(json.dumps({"state": "running", "log_size": 0}))  # re-poll: 'recovered'
        return _frame(json.dumps({"offset": 0, "b64": ""}))


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
    # The event log captures the full lifecycle: submitted → re-assigned → succeeded.
    assert record.events[0].reason == "submitted"
    assert any(
        e.reason == "runtime reclaimed; re-assigned" and e.incarnation == 2 for e in record.events
    )
    assert record.events[-1].to_state is JobState.SUCCEEDED
    # The global audit ledger records the lifecycle: submit (inc 1) + resume (inc 2).
    actions = [(e.action, e.incarnation) for e in store.list_audit(job_id=info.id)]
    assert ("submit", 1) in actions and ("resume", 2) in actions


async def test_resume_stitches_log_with_incarnation_marker(
    store: StateStore, tmp_path: Path
) -> None:
    t = ScriptedTransport()
    backend = _backend(t, store, tmp_path)
    info = await backend.submit(JobSpec(code="train()", resumable=True, name="j"))

    t.reclaim_next_poll = True
    await backend.status(info.id)  # poll reclaims → resume (incarnation 1 → 2)

    logs = await backend.logs(info.id)
    # The re-assign is visible in the stitched log instead of a silent reset to zero.
    assert "incarnation 1 runtime reclaimed; resuming as incarnation 2" in logs


async def test_non_resumable_job_surfaces_reclaim(store: StateStore, tmp_path: Path) -> None:
    t = ScriptedTransport()
    backend = _backend(t, store, tmp_path)
    info = await backend.submit(JobSpec(code="train()", resumable=False, name="j"))

    t.reclaim_next_poll = True
    with pytest.raises(RuntimeUnavailableError):
        await backend.status(info.id)
    assert t.allocate_calls == 1  # did NOT re-allocate
    assert t.launch_calls == 1


async def test_flapping_runtime_is_bounded_not_a_cost_runaway(
    store: StateStore, tmp_path: Path
) -> None:
    """A runtime reclaimed on every cycle must NOT re-allocate paid GPUs forever — the
    incarnation cap stops it and marks the job failed (the worst footgun, guarded)."""
    t = FlappingTransport()
    backend = _backend(t, store, tmp_path, max_incarnations=3)
    info = await backend.submit(JobSpec(code="train()", resumable=True, name="j"))
    assert t.allocate_calls == 1  # initial allocation only

    with pytest.raises(AllocationError, match="exceeded the cap of 3"):
        await backend.result(info.id)

    # Bounded: initial + (max_incarnations - 1) resumes, then it refuses to re-allocate.
    assert t.allocate_calls == 3
    record = store.get_job(info.id)
    assert record is not None
    assert record.incarnations == 3
    assert record.state is JobState.FAILED  # terminal, not stuck RUNNING
    assert any(
        e.to_state is JobState.FAILED and e.reason == "exceeded max incarnations"
        for e in record.events
    )
