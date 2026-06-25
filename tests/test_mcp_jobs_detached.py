"""MCP detached-job tools over a real (local-subprocess) DetachedColabBackend."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from colabctl.jobs.backend import DetachedColabBackend
from colabctl.mcp_server import DetachedJobTools
from colabctl.state import StateStore
from conftest import LocalExecTransport


@pytest.fixture
def tools(tmp_path: Path) -> DetachedJobTools:
    store = StateStore(home=tmp_path / "home")
    root = str(tmp_path / "jobs")
    return DetachedJobTools(
        backend_factory=lambda: DetachedColabBackend(
            LocalExecTransport(), state=store, root=root, poll_interval=0.05
        )
    )


async def _await_done(tools: DetachedJobTools, job_id: str, *, tries: int = 200) -> None:
    for _ in range(tries):
        if (await tools.job_status(job_id))["state"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("job never finished")


async def test_submit_poll_collect(tools: DetachedJobTools) -> None:
    submitted = await tools.submit_job("print('mcp detached')")
    assert submitted["state"] == "RUNNING"
    # MCP Tasks-shaped (9.1): a durable taskId + spec status alongside the legacy fields.
    assert submitted["taskId"] == submitted["id"] and submitted["status"] == "working"
    job_id = submitted["id"]

    await _await_done(tools, job_id)
    result = await tools.job_result(job_id)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "mcp detached" in result["stdout"]
    assert result["status"] == "completed" and result["taskId"] == job_id


def test_task_status_maps_jobstate_to_sep1686():
    from colabctl.backends.base import JobState
    from colabctl.mcp_server import _task_fields

    assert _task_fields("j", JobState.RUNNING) == {"taskId": "j", "status": "working"}
    assert _task_fields("j", JobState.SUCCEEDED)["status"] == "completed"
    assert _task_fields("j", JobState.FAILED)["status"] == "failed"
    assert _task_fields("j", JobState.CANCELLED)["status"] == "cancelled"


async def test_coded_wrapper_enriches_errors_and_passes_success():
    from colabctl.errors import ColabctlError, QuotaExceededError
    from colabctl.mcp_server import _coded

    async def boom() -> None:
        raise QuotaExceededError("out of compute units")

    with pytest.raises(ColabctlError) as ei:
        await _coded(boom)()
    msg = str(ei.value)
    assert "code=QUOTA_EXCEEDED" in msg and "category=allocation" in msg and "remediation:" in msg

    async def ok() -> dict:
        return {"x": 1}

    assert await _coded(ok)() == {"x": 1}  # success path untouched


async def test_job_logs_offset(tools: DetachedJobTools) -> None:
    submitted = await tools.submit_job("print('LINE1')\nprint('LINE2')")
    await _await_done(tools, submitted["id"])
    first = await tools.job_logs(submitted["id"], offset=0)
    assert "LINE1" in first["text"]
    # Resuming at the returned offset yields no duplicate output.
    again = await tools.job_logs(submitted["id"], offset=first["offset"])
    assert again["text"] == ""


async def test_cancel_job(tools: DetachedJobTools) -> None:
    submitted = await tools.submit_job("import time; time.sleep(60)")
    for _ in range(200):
        if (await tools.job_status(submitted["id"]))["state"] == "RUNNING":
            break
        await asyncio.sleep(0.05)
    assert "cancelled" in await tools.cancel_job(submitted["id"])
    assert (await tools.job_status(submitted["id"]))["state"] == "CANCELLED"
