"""Job reconcile + TTL gc + rm (Phase 1.6.4): stop the record leak; honest LOST state.

delete_job had zero callers and only sessions were reconciled, so terminal/orphaned job
records accumulated forever and a job whose runtime was reclaimed lied RUNNING. gc_jobs
fixes both, and `job rm` wires the single-record delete.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.backends.base import JobState
from colabctl.jobs.backend import DetachedColabBackend
from colabctl.models import RuntimeSpec
from colabctl.state import JobEvent, StateStore, StoredJob, utcnow
from conftest import FakeTransport

runner = CliRunner()


def _job(
    jid: str,
    state: JobState,
    *,
    resumable: bool = False,
    session: str = "s",
    terminal_age_hours: float = 0.0,
) -> StoredJob:
    at = utcnow() - timedelta(hours=terminal_age_hours)
    return StoredJob(
        id=jid,
        backend="colab",
        state=state,
        session_name=session,
        resumable=resumable,
        events=[JobEvent(from_state=JobState.RUNNING, to_state=state, incarnation=1, at=at)],
    )


async def test_gc_prunes_old_terminal_records(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    store.put_job(_job("old", JobState.SUCCEEDED, terminal_age_hours=200))  # > 168h default
    store.put_job(_job("recent", JobState.SUCCEEDED, terminal_age_hours=1))  # within TTL
    backend = DetachedColabBackend(FakeTransport(), state=store)
    report = await backend.gc_jobs()
    assert report.pruned == ["old"]
    assert store.get_job("old") is None
    assert store.get_job("recent") is not None


async def test_gc_reconciles_dead_nonresumable_job(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    store.put_job(_job("ghost", JobState.RUNNING, resumable=False, session="gone"))
    backend = DetachedColabBackend(FakeTransport(), state=store)  # no live sessions
    report = await backend.gc_jobs()
    assert report.reconciled == ["ghost"]
    rec = store.get_job("ghost")
    assert rec is not None and rec.state is JobState.FAILED
    assert rec.events[-1].reason == "runtime gone (reconciled by gc)"


async def test_gc_leaves_resumable_job_alone(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    store.put_job(_job("res", JobState.RUNNING, resumable=True, session="gone"))
    backend = DetachedColabBackend(FakeTransport(), state=store)
    report = await backend.gc_jobs()
    assert report.reconciled == []  # resumable jobs recover on poll — never gc'd to FAILED
    rec = store.get_job("res")
    assert rec is not None and rec.state is JobState.RUNNING


async def test_gc_keeps_running_job_with_live_session(tmp_path: Path) -> None:
    store = StateStore(home=tmp_path / "h")
    t = FakeTransport()
    await t.allocate(RuntimeSpec(name="live"))  # the session is live in list_sessions()
    store.put_job(_job("ok", JobState.RUNNING, resumable=False, session="live"))
    backend = DetachedColabBackend(t, state=store)
    report = await backend.gc_jobs()
    assert report.reconciled == []
    rec = store.get_job("ok")
    assert rec is not None and rec.state is JobState.RUNNING


def test_job_rm_deletes_record() -> None:
    StateStore().put_job(StoredJob(id="r1"))
    result = runner.invoke(cli_mod.app, ["job", "rm", "r1"])
    assert result.exit_code == 0 and "removed r1" in result.output
    assert StateStore().get_job("r1") is None


def test_job_rm_unknown_errors() -> None:
    result = runner.invoke(cli_mod.app, ["job", "rm", "nope"])
    assert result.exit_code == 1
    assert "no such job" in result.output
