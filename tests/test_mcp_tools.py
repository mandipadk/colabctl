"""Tests for the MCP tool logic (ColabTools) over the FakeTransport-backed client."""

from __future__ import annotations

from colabctl.mcp_server import ColabTools
from colabctl.sdk import ColabClient
from conftest import FakeTransport


def _tools() -> tuple[ColabTools, FakeTransport]:
    t = FakeTransport()
    return ColabTools(ColabClient(transport=t)), t


async def test_allocate_runtime_returns_info_dict():
    tools, _ = _tools()
    info = await tools.allocate_runtime(gpu="T4", name="agent-job")
    assert info["name"] == "agent-job"
    assert info["accelerator"] == "T4"
    assert info["hardware"] == "T4"
    assert info["status"] == "IDLE"


async def test_allocate_runtime_is_kept():
    tools, t = _tools()
    await tools.allocate_runtime(name="j")
    # keep=True under the hood — the agent owns teardown.
    assert t.stopped == []
    assert "j" in t.sessions


async def test_run_code_returns_result_dict():
    tools, t = _tools()
    await tools.allocate_runtime(name="j")
    out = await tools.run_code("j", "print('hello')")
    assert out["ok"] is True
    assert out["status"] == "ok"
    assert "ran:" in out["stdout"]
    assert out["error"] is None
    assert t.executed == [("j", "print('hello')")]


async def test_run_once_allocates_runs_and_tears_down():
    tools, t = _tools()
    out = await tools.run_once("print('one-shot')", gpu="T4")
    assert out["ok"] is True and out["status"] == "ok"
    assert "ran:" in out["stdout"]
    # exactly one runtime was used and it was released (the one-shot dance, no leak)
    assert len(t.executed) == 1
    assert t.executed[0][0] in t.stopped


async def test_run_file_reads_local_file_and_runs_once(tmp_path):
    tools, t = _tools()
    script = tmp_path / "script.py"
    script.write_text("print('from file')")
    out = await tools.run_file(str(script), gpu="T4")
    assert out["ok"] is True
    assert t.executed[0][1] == "print('from file')"  # the file's code ran
    assert t.executed[0][0] in t.stopped  # and was torn down


async def test_list_and_status():
    tools, _ = _tools()
    await tools.allocate_runtime(name="a")
    await tools.allocate_runtime(name="b")
    names = {r["name"] for r in await tools.list_runtimes()}
    assert names == {"a", "b"}
    assert (await tools.runtime_status("a"))["name"] == "a"
    assert await tools.runtime_status("missing") is None


async def test_upload_and_download(tmp_path):
    tools, t = _tools()
    await tools.allocate_runtime(name="j")
    local = tmp_path / "in.txt"
    local.write_text("data")
    msg = await tools.upload_file("j", str(local), "content/in.txt")
    assert "uploaded" in msg
    assert t.uploaded == [("j", str(local), "content/in.txt")]

    dest = tmp_path / "out.txt"
    msg = await tools.download_file("j", "content/in.txt", str(dest))
    assert "downloaded" in msg
    assert dest.read_text() == "downloaded"


async def test_stop_runtime():
    tools, t = _tools()
    await tools.allocate_runtime(name="j")
    msg = await tools.stop_runtime("j")
    assert msg == "stopped j"
    assert t.stopped == ["j"]


async def test_interrupt_runtime():
    tools, t = _tools()
    await tools.allocate_runtime(name="j")
    msg = await tools.interrupt_runtime("j")
    assert msg == "interrupted j"
    assert t.interrupts == ["j"]
