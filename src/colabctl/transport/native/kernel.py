"""Native Jupyter kernel client + output normalization.

Wraps ``jupyter-kernel-client`` (the same library the official CLI uses) against
a Colab runtime-proxy URL/token, using the verified header recipe. The
output-normalization functions (raw nbformat dict → typed :class:`Output`) and the
file-transfer code builders/parsers are pure and unit-tested offline; the live
kernel connection is exercised only against a real runtime.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Protocol

from colabctl.errors import FileTransferError
from colabctl.models import (
    DisplayDataOutput,
    ErrorOutput,
    ExecuteResultOutput,
    ExecutionResult,
    Output,
    StreamOutput,
)
from colabctl.transport.base import OutputCallback
from colabctl.transport.native.client import CLIENT_AGENT, ColabBackendClient

# Markers framing a base64 file payload printed by the download helper.
_B64_BEGIN = "<<<COLABCTL_B64>>>"
_B64_END = "<<<COLABCTL_END>>>"


# --- pure output mapping (offline-tested) -----------------------------------


def _as_text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(v) for v in value)
    return str(value)


def normalize_output(raw: dict[str, Any]) -> Output | None:
    """Map one nbformat-style output dict to a typed :class:`Output` (or ``None``)."""
    kind = raw.get("output_type")
    if kind == "stream":
        name = raw.get("name", "stdout")
        return StreamOutput(
            name="stderr" if name == "stderr" else "stdout",
            text=_as_text(raw.get("text", "")),
        )
    if kind == "execute_result":
        return ExecuteResultOutput(
            data=dict(raw.get("data", {})),
            metadata=dict(raw.get("metadata", {})),
            execution_count=raw.get("execution_count"),
        )
    if kind == "display_data":
        return DisplayDataOutput(
            data=dict(raw.get("data", {})),
            metadata=dict(raw.get("metadata", {})),
        )
    if kind == "error":
        return ErrorOutput(
            ename=raw.get("ename", ""),
            evalue=raw.get("evalue", ""),
            traceback=list(raw.get("traceback", [])),
        )
    return None


def outputs_to_result(reply: dict[str, Any]) -> ExecutionResult:
    """Build an :class:`ExecutionResult` from a kernel ``execute`` reply dict."""
    outputs: list[Output] = []
    for raw in reply.get("outputs", []):
        mapped = normalize_output(raw)
        if mapped is not None:
            outputs.append(mapped)
    status = reply.get("status")
    if status not in ("ok", "error", "abort"):
        status = "error" if any(isinstance(o, ErrorOutput) for o in outputs) else "ok"
    return ExecutionResult(
        status=status,
        execution_count=reply.get("execution_count"),
        outputs=outputs,
    )


# --- file-transfer code builders/parsers (offline-tested) -------------------


def build_upload_code(remote_path: str, b64data: str) -> str:
    """Code that writes a base64-encoded blob to ``remote_path`` on the VM."""
    return (
        "import base64, pathlib\n"
        f"_p = pathlib.Path({json.dumps(remote_path)})\n"
        "_p.parent.mkdir(parents=True, exist_ok=True)\n"
        f"_p.write_bytes(base64.b64decode({json.dumps(b64data)}))\n"
        "print('COLABCTL_UPLOAD_OK')\n"
    )


def build_download_code(remote_path: str) -> str:
    """Code that prints ``remote_path``'s bytes as a marker-framed base64 string."""
    begin, end = json.dumps(_B64_BEGIN), json.dumps(_B64_END)
    return (
        "import base64\n"
        f"with open({json.dumps(remote_path)}, 'rb') as _f:\n"
        "    _d = _f.read()\n"
        f"print({begin} + base64.b64encode(_d).decode() + {end})\n"
    )


def parse_b64_payload(text: str) -> bytes:
    """Extract and decode the base64 payload framed by the download markers."""
    start = text.find(_B64_BEGIN)
    end = text.find(_B64_END, start + len(_B64_BEGIN)) if start != -1 else -1
    if start == -1 or end == -1:
        raise FileTransferError("Download payload markers not found in kernel output.")
    encoded = text[start + len(_B64_BEGIN) : end].strip()
    try:
        return base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise FileTransferError("Could not decode downloaded payload.") from exc


# --- kernel protocol + live implementation ----------------------------------


class KernelProtocol(Protocol):
    """What the native transport needs from a kernel (so it can be faked in tests)."""

    async def start(self) -> None: ...
    async def execute(
        self, code: str, *, timeout: float | None = None, on_output: OutputCallback | None = None
    ) -> ExecutionResult: ...
    async def restart(self) -> None: ...
    async def stop(self) -> None: ...


class NativeKernel:
    """Live Jupyter kernel over a Colab runtime proxy (verified recipe).

    ``jupyter-kernel-client`` is synchronous; calls run in a worker thread.
    """

    def __init__(self, url: str, token: str, *, kernel_id: str | None = None) -> None:
        self._url = url
        self._token = token
        self._kernel_id = kernel_id
        # Typed Any (not Any | None) so the lazy jupyter-kernel-client object's
        # attributes don't trip mypy's union-attr check in the sync helpers.
        self._client: Any = None

    async def start(self) -> None:
        if self._client is None:
            self._client = await asyncio.to_thread(self._build_and_start)

    async def execute(
        self,
        code: str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        if self._client is None:
            await self.start()
        reply = await asyncio.to_thread(self._execute_sync, code, timeout, on_output)
        return outputs_to_result(reply)

    async def restart(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.restart)

    async def stop(self) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._stop_sync)
            self._client = None

    # -- sync internals (run in a thread) -----------------------------------

    def _build_and_start(self) -> Any:
        import jupyter_kernel_client as jkc

        client = jkc.KernelClient(
            server_url=self._url,
            token=self._token,
            kernel_id=self._kernel_id,
            client_kwargs={
                "subprotocol": jkc.JupyterSubprotocol.DEFAULT,
                "extra_params": ColabBackendClient.proxy_ws_params(self._token),
            },
            headers=ColabBackendClient.proxy_kernel_headers(self._token),
        )
        # Don't let closing the client tear down the kernel; we manage lifecycle.
        client._own_kernel = False
        client.start()
        if getattr(client, "id", None):
            self._kernel_id = client.id
        return client

    def _execute_sync(
        self, code: str, timeout: float | None, on_output: OutputCallback | None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if on_output is None:
            reply = self._client.execute(code, **kwargs)
            return reply or {}

        # Streaming: jupyter-kernel-client's default_output_hook accumulates nbformat
        # outputs as iopub messages arrive; we forward each new one to on_output in
        # real time. (Same mechanism the official google-colab-cli uses.)
        from jupyter_kernel_client.client import (
            output_hook as default_output_hook,
        )

        outputs: list[dict[str, Any]] = []

        def streaming_hook(msg: dict[str, Any]) -> None:
            new_indexes = default_output_hook(outputs, msg)
            for idx in sorted(new_indexes or []):
                if idx < len(outputs):
                    mapped = normalize_output(outputs[idx])
                    if mapped is not None:
                        on_output(mapped)

        reply = self._client.execute_interactive(code, output_hook=streaming_hook, **kwargs)
        content = (reply or {}).get("content", {})
        return {
            "outputs": outputs,
            "status": content.get("status"),
            "execution_count": content.get("execution_count"),
        }

    def _stop_sync(self) -> None:
        client = self._client._manager.client
        client.stop_channels()
        if getattr(client, "kernel_socket", None):
            client.kernel_socket.close()


def default_kernel_factory(url: str, token: str) -> KernelProtocol:
    """Factory the native transport uses to build live kernels (overridable in tests)."""
    return NativeKernel(url, token)


__all__ = [
    "CLIENT_AGENT",
    "KernelProtocol",
    "NativeKernel",
    "build_download_code",
    "build_upload_code",
    "default_kernel_factory",
    "normalize_output",
    "outputs_to_result",
    "parse_b64_payload",
]
