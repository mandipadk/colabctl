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
    job_id = submitted["id"]

    await _await_done(tools, job_id)
    result = await tools.job_result(job_id)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "mcp detached" in result["stdout"]


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
