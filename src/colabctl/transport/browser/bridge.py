"""Browser-bridge transport + its JSON-RPC relay.

``_JsonRpcClient`` is a tiny request/response relay over a connected WebSocket
(id-correlated, fully unit-tested with a fake peer). ``BrowserBridgeTransport``
starts a local WebSocket server, opens a Colab tab pointed back at it (the colab-mcp
``#mcpProxyToken=…&mcpProxyPort=…`` fragment), and maps the ``TransportAdapter``
operations onto JSON-RPC calls the Colab frontend services.

The JSON-RPC method/result shapes below follow the documented colab-mcp model; they
are NOT yet confirmed against the live Colab frontend (see package docstring).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import webbrowser
from typing import Any

from colabctl.errors import RuntimeUnavailableError, TransportError
from colabctl.models import ExecutionResult, RuntimeSpec, SessionInfo, SessionStatus
from colabctl.observability import get_logger
from colabctl.transport.base import Capabilities, OutputCallback, TransportAdapter
from colabctl.transport.native.kernel import outputs_to_result

_log = get_logger("transport.browser")
_DEFAULT_COLAB_HOST = "https://colab.research.google.com"


class _JsonRpcClient:
    """Minimal id-correlated JSON-RPC client over a connected WebSocket.

    ``ws`` must support ``await ws.send(str)`` and async iteration yielding str.
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is None or fut.done():
                    continue
                error = msg.get("error")
                if error is not None:
                    fut.set_exception(TransportError(f"browser bridge error: {error}"))
                else:
                    fut.set_result(msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(TransportError(f"browser bridge connection lost: {exc}"))
        else:
            # Clean peer close: fail any still-pending calls now so callers don't
            # hang until their per-call timeout fires.
            self._fail_pending(TransportError("browser bridge connection closed"))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 120.0
    ) -> Any:
        self._next_id += 1
        msg_id = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(
            json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        )
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise TransportError(
                f"browser bridge call {method!r} timed out after {timeout}s"
            ) from exc

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None


class BrowserBridgeTransport(TransportAdapter):
    """Drive Colab via a logged-in browser tab (colab-mcp relay model)."""

    name = "browser"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        colab_host: str = _DEFAULT_COLAB_HOST,
        open_browser: bool = True,
        connect_timeout: float = 120.0,
        _rpc: _JsonRpcClient | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._colab_host = colab_host
        self._open_browser = open_browser
        self._connect_timeout = connect_timeout
        self._token = secrets.token_urlsafe(24)
        self._rpc = _rpc
        self._server: Any | None = None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            interactive=True,
            streaming_output=False,
            headless=False,  # requires an open, logged-in browser tab
            selectable_accelerator=True,
            keepalive=False,
            file_transfer=True,
            notebook_execution=False,
            caveats=[
                "Human-in-the-loop: needs a logged-in Colab browser tab open (not headless).",
                "Sanctioned (Google's own colab-mcp model), but NOT live-validated — the "
                "JSON-RPC request/result shapes follow the documented model and must be "
                "confirmed against the live Colab frontend.",
            ],
        )

    async def start(self) -> SessionInfo | None:
        """Start the local relay, open a Colab tab, and await the frontend connection."""
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - only without the extra
            raise TransportError(
                "websockets is not installed. Install with `pip install 'colabctl[browser]'`."
            ) from exc

        connected: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        async def handler(ws: Any) -> None:
            # Origin restriction + token handshake before we trust the peer.
            origin = ws.request.headers.get("Origin", "") if hasattr(ws, "request") else ""
            if origin and not origin.startswith(self._colab_host):
                await ws.close()
                return
            hello = json.loads(await ws.recv())
            if hello.get("token") != self._token:
                await ws.close()
                return
            if not connected.done():
                connected.set_result(ws)
            await ws.wait_closed()

        self._server = await websockets.serve(handler, self._host, self._port)
        bound_port = self._server.sockets[0].getsockname()[1]
        url = (
            f"{self._colab_host}/notebooks/empty.ipynb"
            f"#mcpProxyToken={self._token}&mcpProxyPort={bound_port}"
        )
        _log.info("Browser-bridge: open this Colab tab to connect:\n%s", url)
        if self._open_browser:
            webbrowser.open(url)
        ws = await asyncio.wait_for(connected, self._connect_timeout)
        self._rpc = _JsonRpcClient(ws)
        self._rpc.start()
        return None

    def _client(self) -> _JsonRpcClient:
        if self._rpc is None:
            raise RuntimeUnavailableError("Browser bridge not started; call start() first.")
        return self._rpc

    async def allocate(self, spec: RuntimeSpec) -> SessionInfo:
        result = await self._client().call(
            "allocateRuntime", {"accelerator": spec.accelerator.value, "name": spec.name}
        )
        return self._session_info(result)

    async def list_sessions(self) -> list[SessionInfo]:
        result = await self._client().call("listRuntimes")
        return [self._session_info(r) for r in (result or [])]

    async def status(self, name: str) -> SessionInfo | None:
        result = await self._client().call("runtimeStatus", {"session": name})
        return self._session_info(result) if result else None

    async def execute(
        self,
        name: str,
        code: str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        result = await self._client().call(
            "execute", {"session": name, "code": code}, timeout=timeout or self._connect_timeout
        )
        execution = outputs_to_result(result or {})
        if on_output is not None:
            for output in execution.outputs:
                on_output(output)
        return execution

    async def upload(self, name: str, local_path: Any, remote_path: str) -> None:
        from pathlib import Path

        data = base64.b64encode(Path(local_path).read_bytes()).decode()
        await self._client().call(
            "uploadFile", {"session": name, "remote": remote_path, "dataB64": data}
        )

    async def download(self, name: str, remote_path: str, local_path: Any) -> None:
        from pathlib import Path

        result = await self._client().call("downloadFile", {"session": name, "remote": remote_path})
        data = base64.b64decode((result or {}).get("dataB64", ""))
        Path(local_path).write_bytes(data)

    async def stop(self, name: str) -> None:
        await self._client().call("stopRuntime", {"session": name})

    async def aclose(self) -> None:
        if self._rpc is not None:
            await self._rpc.close()
            self._rpc = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _session_info(result: dict[str, Any]) -> SessionInfo:
        from colabctl.models import Accelerator, Variant

        acc = result.get("accelerator", "NONE")
        var = result.get("variant", "")
        status = result.get("status", "")
        return SessionInfo(
            name=result.get("name", ""),
            endpoint=result.get("endpoint", ""),
            accelerator=Accelerator(acc) if acc in Accelerator.__members__ else Accelerator.NONE,
            variant=Variant(var) if var in Variant.__members__ else Variant.DEFAULT,
            status=SessionStatus(status)
            if status in SessionStatus.__members__
            else SessionStatus.UNKNOWN,
        )
