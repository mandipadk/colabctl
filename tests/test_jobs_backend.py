"""DetachedColabBackend end-to-end, hermetically.

Uses ``LocalExecTransport`` (runs the emitted payloads as real subprocesses) plus a
shared on-disk ``StateStore``, so the durability claim is actually proven: a job
submitted via one backend instance is observable/collectable from a second instance
sharing only the store — the stand-in for a second ``colabctl`` process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from colabctl.backends.base import JobSpec, JobState
from colabctl.jobs.backend import DetachedColabBackend
from colabctl.state import StateStore
from conftest import LocalExecTransport


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(home=tmp_path / "home")


def _backend(store: StateStore, tmp_path: Path) -> DetachedColabBackend:
    return DetachedColabBackend(
        LocalExecTransport(), state=store, root=str(tmp_path / "jobs"), poll_interval=0.05
    )


async def _await_terminal(backend: DetachedColabBackend, job_id: str, *, tries=200, delay=0.05):
    for _ in range(tries):
        info = await backend.status(job_id)
        if info.state.is_terminal:
            return info
        await asyncio.sleep(delay)
    raise AssertionError(f"job {job_id} never reached a terminal state")


async def test_submit_persists_and_runs(store: StateStore, tmp_path: Path) -> None:
    backend = _backend(store, tmp_path)
    info = await backend.submit(JobSpec(code="print('hello detached')", name="j"))
    assert info.state is JobState.RUNNING
    # Persisted immediately — survives this backend instance.
    record = store.get_job(info.id)
    assert record is not None and record.pid and record.remote_dir
    assert record.code == "print('hello detached')"

    await _await_terminal(backend, info.id)
    result = await backend.result(info.id)
    assert result.state is JobState.SUCCEEDED
    assert result.exit_code == 0
    assert "hello detached" in result.stdout


async def test_collectable_from_a_second_process(store: StateStore, tmp_path: Path) -> None:
    submitter = _backend(store, tmp_path)
    info = await submitter.submit(JobSpec(code="print('cross process')", name="j"))

    # A second backend sharing only the store == a second process.
    collector = DetachedColabBackend(
        LocalExecTransport(), state=store, root=str(tmp_path / "jobs"), poll_interval=0.05
    )
    await _await_terminal(collector, info.id)
    assert "cross process" in await collector.logs(info.id)
    result = await collector.result(info.id)
    assert result.state is JobState.SUCCEEDED


async def test_log_tail_resumes_from_persisted_offset(store: StateStore, tmp_path: Path) -> None:
    backend = _backend(store, tmp_path)
    info = await backend.submit(JobSpec(code="print('AAAA')\nprint('BBBB')", name="j"))
    await _await_terminal(backend, info.id)

    first, off1 = await backend.log_tail(info.id)
    assert "AAAA" in first
    assert store.get_job(info.id).log_offset == off1  # offset persisted
    # A follow-up read (default offset = persisted) returns only what's new (nothing).
    more, off2 = await backend.log_tail(info.id)
    assert more == "" and off2 == off1


async def test_failed_job_reports_failure_and_error(store: StateStore, tmp_path: Path) -> None:
    backend = _backend(store, tmp_path)
    info = await backend.submit(JobSpec(code="import sys\nprint('boom')\nsys.exit(3)", name="j"))
    await _await_terminal(backend, info.id)
    result = await backend.result(info.id)
    assert result.state is JobState.FAILED
    assert result.exit_code == 3
    assert result.error and "boom" in result.error


async def test_cancel_running_job(store: StateStore, tmp_path: Path) -> None:
    backend = _backend(store, tmp_path)
    info = await backend.submit(JobSpec(code="import time\ntime.sleep(60)", name="j"))
    # Wait until it's actually running before cancelling.
    for _ in range(200):
        if (await backend.status(info.id)).state is JobState.RUNNING:
            break
        await asyncio.sleep(0.05)
    await backend.cancel(info.id)
    assert (await backend.status(info.id)).state is JobState.CANCELLED


async def test_list_jobs_from_store(store: StateStore, tmp_path: Path) -> None:
    backend = _backend(store, tmp_path)
    await backend.submit(JobSpec(code="print(1)", name="a"))
    await backend.submit(JobSpec(code="print(2)", name="b"))
    listed = await backend.list_jobs()
    assert len(listed) == 2


async def test_capabilities_are_honest_now(store: StateStore, tmp_path: Path) -> None:
    caps = _backend(store, tmp_path).capabilities
    assert caps.persistent is True  # jobs survive the process (state store)
    assert caps.streaming_logs is True  # logs spool on the VM and tail by offset
