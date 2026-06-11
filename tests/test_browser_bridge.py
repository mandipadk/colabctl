"""Browser-bridge transport over a fake ColabMCP server (no real browser).

The fake speaks the captured ColabMCP protocol and *actually runs* ``run_code_cell`` code
as a local subprocess, so the transport's execute / upload / download / keep-alive paths
are exercised end to end (the "VM" filesystem is local here).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from colabctl.models import RuntimeSpec
from colabctl.transport.browser import BrowserBridgeTransport, McpClient, mcp_text


def _content(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class FakeColabMcpWS:
    """In-memory ColabMCP server over a fake ws; runs `run_code_cell` code for real."""

    def __init__(self) -> None:
        self._out: asyncio.Queue[str] = asyncio.Queue()
        self.cells: dict[str, str] = {}
        self._n = 0

    async def send(self, data: str) -> None:
        msg = json.loads(data)
        mid = msg.get("id")
        if mid is None:  # a notification (e.g. notifications/initialized) — no reply
            return
        result = self._dispatch(msg.get("method"), msg.get("params") or {})
        await self._out.put(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}))

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "ColabMCP", "version": "1.0.0"},
            }
        if method == "tools/list":
            names = ("add_code_cell", "update_cell", "run_code_cell", "delete_cell", "get_cells")
            return {"tools": [{"name": n} for n in names]}
        if method == "tools/call":
            return self._call(params.get("name"), params.get("arguments") or {})
        return {}

    def _call(self, name: str, args: dict) -> dict:
        if name == "add_code_cell":
            self._n += 1
            cid = f"cell-{self._n}"
            self.cells[cid] = args.get("code", "")
            return _content(cid)
        if name == "update_cell":
            self.cells[args["cellId"]] = args.get("content", "")
            return _content("ok")
        if name == "delete_cell":
            self.cells.pop(args.get("cellId"), None)
            return _content("ok")
        if name == "run_code_cell":
            code = self.cells.get(args["cellId"], "")
            proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            return _content(proc.stdout, is_error=proc.returncode != 0)
        if name == "get_cells":
            return _content(json.dumps(self.cells))
        return _content("")

    def __aiter__(self) -> FakeColabMcpWS:
        return self

    async def __anext__(self) -> str:
        return await self._out.get()


async def _transport() -> tuple[BrowserBridgeTransport, FakeColabMcpWS]:
    ws = FakeColabMcpWS()
    client = McpClient(ws)
    client.start()
    await client.initialize()
    return BrowserBridgeTransport(_client=client, open_browser=False), ws


# -- McpClient ----------------------------------------------------------------


async def test_initialize_reports_colab_mcp_server():
    ws = FakeColabMcpWS()
    client = McpClient(ws)
    client.start()
    info = await client.initialize()
    assert info["serverInfo"]["name"] == "ColabMCP"
    assert client.server_info is not None and client.server_info["version"] == "1.0.0"
    tools = await client.list_tools()
    assert "run_code_cell" in {t["name"] for t in tools}
    await client.close()


def test_mcp_text_extracts_text_content():
    assert mcp_text(_content("hello")) == "hello"
    assert mcp_text({"content": [{"type": "image"}]}) == ""
    assert mcp_text({}) == ""


# -- transport ----------------------------------------------------------------


async def test_execute_runs_code_via_run_code_cell():
    t, _ = await _transport()
    await t.allocate(RuntimeSpec(name="b"))
    result = await t.execute("b", "print(6 * 7)")
    assert result.ok and "42" in result.text
    await t.aclose()


async def test_execute_reuses_one_scratch_cell_per_session():
    t, _ = await _transport()
    await t.execute("b", "print(1)")
    await t.execute("b", "print(2)")
    assert len(t._cells) == 1  # update, not a new cell each time
    await t.aclose()


async def test_error_cell_marks_status_error():
    t, _ = await _transport()
    result = await t.execute("b", "import sys; sys.exit(3)")
    assert not result.ok
    await t.aclose()


async def test_upload_download_round_trips(tmp_path):
    t, _ = await _transport()
    await t.allocate(RuntimeSpec(name="b"))
    src = tmp_path / "f.bin"
    src.write_bytes(b"browser-bytes")
    remote = str(tmp_path / "remote.bin")  # the fake's "VM" disk is local
    await t.upload("b", src, remote)
    dest = tmp_path / "out.bin"
    await t.download("b", remote, dest)
    assert dest.read_bytes() == b"browser-bytes"
    await t.aclose()


async def test_keep_alive_runs_a_noop_cell():
    t, _ = await _transport()
    await t.keep_alive("b")  # genuine activity in the authenticated session
    assert t._ka_cell is not None
    await t.aclose()


async def test_stop_deletes_scratch_cell():
    t, ws = await _transport()
    await t.execute("b", "print(1)")
    cell_id = t._cells["b"]
    assert cell_id in ws.cells
    await t.stop("b")
    assert cell_id not in ws.cells  # cleaned up via delete_cell
    assert await t.status("b") is None
    await t.aclose()


async def test_capabilities_advertise_sanctioned_keepalive():
    caps = BrowserBridgeTransport(open_browser=False).capabilities
    assert caps.keepalive is True  # the one transport that can
    assert caps.headless is False and caps.notebook_execution is True


async def test_start_is_noop_when_already_connected():
    t, _ = await _transport()  # already has an injected, connected client
    assert await t.start() is None  # idempotent — does not import websockets / open a tab
    await t.aclose()
