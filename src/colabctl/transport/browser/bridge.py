"""Browser-bridge transport over Colab's "local MCP server" (ColabMCP).

Phase A (2026-06-11) captured the real protocol: Colab connects to a local WebSocket
(subprotocol ``mcp``, token via ``?access_token=``) and acts as the MCP *server*, exposing
notebook tools (``add_code_cell``, ``run_code_cell``, ``get_cells``, …). This transport is
the MCP *client*: it drives a Colab notebook through those tools via the user's logged-in
browser tab — a **sanctioned, first-party** path (not the reverse-engineered ``/tun/m/*``)
that also keeps the runtime alive, because running a no-op cell is genuine kernel activity
in the **authenticated session** (the one keep-alive that works where token auth cannot).

Not headless — it needs an open, logged-in Colab tab — so the CLI/native transports remain
the automated path; this is the human-in-the-loop, sanctioned-with-keepalive option.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from colabctl.errors import FileTransferError, RuntimeUnavailableError, TransportError
from colabctl.models import (
    ExecutionResult,
    RuntimeSpec,
    SessionInfo,
    SessionStatus,
    StreamOutput,
)
from colabctl.observability import get_logger
from colabctl.transport.base import Capabilities, OutputCallback, TransportAdapter
from colabctl.transport.browser.mcp import McpClient, mcp_text
from colabctl.transport.native.kernel import (
    build_download_code,
    build_upload_code,
    parse_b64_payload,
)

_log = get_logger("transport.browser")
_DEFAULT_COLAB_HOST = "https://colab.research.google.com"
_UPLOAD_SENTINEL = "COLABCTL_UPLOAD_OK"
_KEEPALIVE_CODE = "None  # colabctl keep-alive"


class BrowserBridgeTransport(TransportAdapter):
    """Drive a Colab notebook through Colab's own MCP tools, via a logged-in tab."""

    name = "browser"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        colab_host: str = _DEFAULT_COLAB_HOST,
        open_browser: bool = True,
        connect_timeout: float = 120.0,
        _client: McpClient | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._colab_host = colab_host
        self._open_browser = open_browser
        self._connect_timeout = connect_timeout
        self._token = secrets.token_urlsafe(24)
        self._client_obj = _client
        self._server: Any | None = None
        self._sessions: dict[str, SessionInfo] = {}
        self._cells: dict[str, str] = {}  # session name -> scratch code-cell id
        self._ka_cell: str | None = None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            interactive=True,
            streaming_output=False,
            headless=False,
            selectable_accelerator=False,
            keepalive=True,
            file_transfer=True,
            notebook_execution=True,
            caveats=[
                "Sanctioned: drives a Colab notebook via Colab's own MCP tools through your "
                "logged-in browser tab (NOT headless — the tab must stay open).",
                "Keep-alive works here — running a no-op cell is genuine kernel activity in "
                "the authenticated session (what token auth cannot do).",
                "No runtime-terminate tool: stop() cleans up scratch cells; close the tab to "
                "release the VM.",
                "File transfer rides cell execution (base64); large files are not chunked.",
            ],
        )

    async def start(self) -> SessionInfo | None:
        """Start the local MCP server, open a Colab tab, and complete the MCP handshake."""
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - only without the extra
            raise TransportError(
                "websockets is not installed. Install with `pip install 'colabctl[browser]'`."
            ) from exc

        connected: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        async def handler(ws: Any) -> None:
            req = getattr(ws, "request", None)
            origin = req.headers.get("Origin", "") if req is not None else ""
            path = getattr(req, "path", "") if req is not None else ""
            token = (parse_qs(urlsplit(path).query).get("access_token") or [""])[0]
            if (origin and not origin.startswith(self._colab_host)) or token != self._token:
                await ws.close()
                return
            if not connected.done():
                connected.set_result(ws)
            await ws.wait_closed()  # keep the connection open for the MCP client

        self._server = await websockets.serve(
            handler,
            self._host,
            self._port,
            subprotocols=["mcp"],  # type: ignore[list-item]
        )
        port = self._server.sockets[0].getsockname()[1]
        url = (
            f"{self._colab_host}/notebooks/empty.ipynb"
            f"#mcpProxyToken={self._token}&mcpProxyPort={port}"
        )
        _log.info("Browser-bridge: open this Colab tab and Connect:\n%s", url)
        if self._open_browser:
            webbrowser.open(url)
        ws = await asyncio.wait_for(connected, self._connect_timeout)
        client = McpClient(ws)
        client.start()
        await client.initialize()
        self._client_obj = client
        return None

    def _client(self) -> McpClient:
        if self._client_obj is None:
            raise RuntimeUnavailableError("Browser bridge not started; call start() first.")
        return self._client_obj

    # -- contract -----------------------------------------------------------

    async def allocate(self, spec: RuntimeSpec) -> SessionInfo:
        # The runtime is the open notebook's; there is no allocate tool, so this records a
        # handle for the browser session (one notebook backs them all).
        name = spec.name or "browser"
        info = SessionInfo(
            name=name,
            endpoint="browser-notebook",
            accelerator=spec.accelerator,
            status=SessionStatus.IDLE,
        )
        self._sessions[name] = info
        return info

    async def list_sessions(self) -> list[SessionInfo]:
        return list(self._sessions.values())

    async def status(self, name: str) -> SessionInfo | None:
        return self._sessions.get(name)

    async def execute(
        self,
        name: str,
        code: str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        cell_id = await self._scratch_cell(name, code)
        result = await self._client().call_tool(
            "run_code_cell", {"cellId": cell_id}, timeout=timeout
        )
        execution = ExecutionResult(
            status="error" if result.get("isError") else "ok",
            outputs=[StreamOutput(name="stdout", text=mcp_text(result))],
        )
        if on_output is not None:
            for output in execution.outputs:
                on_output(output)
        return execution

    async def keep_alive(self, name: str) -> None:
        """Keep the runtime alive by running a no-op cell — real activity, real session."""
        client = self._client()
        if self._ka_cell is None:
            res = await client.call_tool(
                "add_code_cell", {"cellIndex": 0, "language": "python", "code": _KEEPALIVE_CODE}
            )
            self._ka_cell = mcp_text(res).strip() or None
            if self._ka_cell is None:
                raise TransportError("add_code_cell did not return a cell id for keep-alive.")
        await client.call_tool("run_code_cell", {"cellId": self._ka_cell})

    async def upload(self, name: str, local_path: Path, remote_path: str) -> None:
        b64 = base64.b64encode(Path(local_path).read_bytes()).decode()
        result = await self.execute(name, build_upload_code(remote_path, b64))
        if not result.ok or _UPLOAD_SENTINEL not in result.text:
            raise FileTransferError(
                f"Upload of {local_path} → {remote_path} failed: {result.text[:200]}"
            )

    async def download(self, name: str, remote_path: str, local_path: Path) -> None:
        result = await self.execute(name, build_download_code(remote_path))
        if not result.ok:
            raise FileTransferError(f"Download of {remote_path} failed: {result.text[:200]}")
        Path(local_path).write_bytes(parse_b64_payload(result.text))

    async def stop(self, name: str) -> None:
        self._sessions.pop(name, None)
        cell_id = self._cells.pop(name, None)
        if cell_id is not None and self._client_obj is not None:
            with contextlib.suppress(TransportError):
                await self._client_obj.call_tool("delete_cell", {"cellId": cell_id})

    async def aclose(self) -> None:
        if self._client_obj is not None:
            await self._client_obj.close()
            self._client_obj = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- internals ----------------------------------------------------------

    async def _scratch_cell(self, name: str, code: str) -> str:
        """Get-or-create a reusable code cell for ``name`` and set its contents to ``code``."""
        cell_id = self._cells.get(name)
        if cell_id is None:
            res = await self._client().call_tool(
                "add_code_cell", {"cellIndex": 0, "language": "python", "code": code}
            )
            cell_id = mcp_text(res).strip()
            if not cell_id:
                raise TransportError("add_code_cell did not return a cell id.")
            self._cells[name] = cell_id
        else:
            await self._client().call_tool("update_cell", {"cellId": cell_id, "content": code})
        return cell_id
