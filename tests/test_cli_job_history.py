"""`colabctl job history` prints a job's state-transition timeline (Phase 1.6.5)."""

from __future__ import annotations

from typer.testing import CliRunner

from colabctl import cli as cli_mod
from colabctl.backends.base import JobState
from colabctl.state import JobEvent, StateStore, StoredJob

runner = CliRunner()


def _ev(frm: JobState, to: JobState, inc: int, reason: str | None = None) -> JobEvent:
    return JobEvent(from_state=frm, to_state=to, incarnation=inc, reason=reason)


def test_job_history_prints_timeline() -> None:
    # The autouse conftest isolates $COLABCTL_HOME, so StateStore() here and inside the
    # command resolve to the same store.
    StateStore().put_job(
        StoredJob(
            id="hj",
            events=[
                _ev(JobState.PENDING, JobState.RUNNING, 1, "submitted"),
                _ev(JobState.RUNNING, JobState.RUNNING, 2, "runtime reclaimed; re-assigned"),
                _ev(JobState.RUNNING, JobState.SUCCEEDED, 2),
            ],
        )
    )
    result = runner.invoke(cli_mod.app, ["job", "history", "hj"])
    assert result.exit_code == 0, result.output
    assert "submitted" in result.output
    assert "re-assigned" in result.output
    assert "inc2" in result.output
    assert "RUNNING -> SUCCEEDED" in result.output


def test_job_history_unknown_job_errors() -> None:
    result = runner.invoke(cli_mod.app, ["job", "history", "nope"])
    assert result.exit_code == 1
    assert "no such job" in result.output


def test_job_history_no_events() -> None:
    StateStore().put_job(StoredJob(id="empty"))
    result = runner.invoke(cli_mod.app, ["job", "history", "empty"])
    assert result.exit_code == 0
    assert "no recorded transitions" in result.output
