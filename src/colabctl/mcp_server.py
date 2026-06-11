"""MCP server exposing colabctl to AI agents (Claude / Codex / any MCP client).

A thin layer over the SDK: each MCP tool maps to a :class:`ColabTools` method that
returns JSON-friendly data. The tool *logic* lives in ``ColabTools`` (no ``mcp``
dependency, fully unit-tested); ``build_server`` lazily imports FastMCP and registers
the tools. Run with ``colabctl-mcp`` (stdio).

Design notes:
- Agent-allocated runtimes are created with ``keep=True`` — the agent owns the
  lifecycle and must call ``stop_runtime`` explicitly (an MCP request shouldn't tear
  down a runtime the agent still wants).
- Results are plain dicts/strings so they serialize cleanly back to the agent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from colabctl.backends.base import Backend, JobSpec
from colabctl.backends.factory import BACKEND_NAMES, build_backend
from colabctl.errors import ConfigurationError
from colabctl.models import Accelerator, ExecutionResult, SessionInfo
from colabctl.sdk.client import ColabClient

if TYPE_CHECKING:
    from colabctl.jobs.backend import DetachedColabBackend

SERVER_INSTRUCTIONS = (
    "Drive Google Colab: allocate GPU runtimes, run code, move files. "
    "Allocate once, reuse the returned session name across run_code calls, and "
    "stop_runtime when done. Runtimes are ephemeral — persist important artifacts "
    "with download_file. Prefer T4 for cheap work; A100/H100 may be unavailable. "
    "For long-running work, prefer the detached job tools: submit_job returns a job id "
    "immediately; then poll job_status/job_logs and collect job_result — the job "
    "survives across calls and disconnects, so do other work between polls instead of "
    "blocking on run_job."
)


def _session_dict(info: SessionInfo) -> dict[str, Any]:
    return {
        "name": info.name,
        "endpoint": info.endpoint,
        "accelerator": info.accelerator.value,
        "hardware": info.hardware_label,
        "variant": info.variant.value,
        "status": info.status.value,
    }


def _result_dict(result: ExecutionResult) -> dict[str, Any]:
    err = result.error
    return {
        "ok": result.ok,
        "status": result.status,
        "text": result.text,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "error": (
            {"ename": err.ename, "evalue": err.evalue, "traceback": err.traceback}
            if err is not None
            else None
        ),
    }


class ColabTools:
    """The MCP tool implementations, bound to a :class:`ColabClient`."""

    def __init__(self, client: ColabClient) -> None:
        self._client = client

    async def allocate_runtime(self, gpu: str = "T4", name: str | None = None) -> dict[str, Any]:
        """Allocate a Colab runtime (kept running) and return its session info."""
        session = await self._client.allocate(gpu=gpu, name=name, keep=True)
        info = await session.status() or session.info
        if info is None:
            return {"name": session.name, "status": "READY"}
        return _session_dict(info)

    async def run_code(
        self, session: str, code: str, timeout: float | None = None
    ) -> dict[str, Any]:
        """Run Python ``code`` on an existing runtime; return outputs + status."""
        result = await self._client.attach(session).run(code, timeout=timeout)
        return _result_dict(result)

    async def list_runtimes(self) -> list[dict[str, Any]]:
        """List active runtimes."""
        return [_session_dict(info) for info in await self._client.list_sessions()]

    async def runtime_status(self, session: str) -> dict[str, Any] | None:
        """Return one runtime's status, or null if unknown."""
        info = await self._client.attach(session).status()
        return _session_dict(info) if info is not None else None

    async def upload_file(self, session: str, local_path: str, remote_path: str) -> str:
        """Upload a local file to the runtime."""
        await self._client.attach(session).upload(local_path, remote_path)
        return f"uploaded {local_path} -> {remote_path}"

    async def download_file(self, session: str, remote_path: str, local_path: str) -> str:
        """Download a file from the runtime to the local machine."""
        await self._client.attach(session).download(remote_path, local_path)
        return f"downloaded {remote_path} -> {local_path}"

    async def interrupt_runtime(self, session: str) -> str:
        """Interrupt the running cell on a runtime without killing it (native transport)."""
        await self._client.attach(session).interrupt()
        return f"interrupted {session}"

    async def stop_runtime(self, session: str) -> str:
        """Stop a runtime and release it."""
        await self._client.attach(session).stop()
        return f"stopped {session}"


def _accelerator(gpu: str) -> Accelerator:
    if gpu.lower() == "none":
        return Accelerator.NONE
    try:
        return Accelerator(gpu.upper())
    except ValueError as exc:
        raise ConfigurationError(f"Unknown accelerator {gpu!r}.") from exc


class JobTools:
    """Batch-job MCP tools over the provider abstraction (Colab / Modal / Vertex)."""

    def __init__(self, backend_factory: Callable[[str], Backend] = build_backend) -> None:
        self._factory = backend_factory
        self._cache: dict[str, Backend] = {}

    def _backend(self, name: str) -> Backend:
        if name not in self._cache:
            self._cache[name] = self._factory(name)
        return self._cache[name]

    async def run_job(
        self,
        backend: str,
        code: str,
        gpu: str = "T4",
        requirements: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run Python ``code`` on a backend (colab/modal/vertex) and return the result."""
        spec = JobSpec(
            code=code,
            accelerator=_accelerator(gpu),
            requirements=requirements or [],
            timeout=timeout,
        )
        result = await self._backend(backend).run(spec)
        return {
            "backend": result.backend,
            "state": result.state.value,
            "ok": result.ok,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.error,
        }

    async def list_backends(self) -> list[dict[str, Any]]:
        """List available backends and their capabilities."""
        out: list[dict[str, Any]] = []
        for name in BACKEND_NAMES:
            caps = self._backend(name).capabilities
            out.append(
                {
                    "name": name,
                    "accelerators": sorted(set(caps.accelerators)),
                    "tos_posture": caps.tos_posture,
                    "interactive": caps.interactive,
                    "notes": caps.notes,
                }
            )
        return out

    async def aclose(self) -> None:
        for backend in self._cache.values():
            await backend.aclose()


def _default_detached_backend() -> DetachedColabBackend:
    from colabctl.jobs.backend import DetachedColabBackend

    return DetachedColabBackend.create()


class DetachedJobTools:
    """Durable detached-job MCP tools (submit → poll → collect, native Colab)."""

    def __init__(
        self, backend_factory: Callable[[], DetachedColabBackend] = _default_detached_backend
    ) -> None:
        self._factory = backend_factory
        self._backend: DetachedColabBackend | None = None

    def _b(self) -> DetachedColabBackend:
        if self._backend is None:
            self._backend = self._factory()
        return self._backend

    async def submit_job(
        self,
        code: str,
        gpu: str = "T4",
        requirements: list[str] | None = None,
        timeout: int | None = None,
        resumable: bool = False,
    ) -> dict[str, Any]:
        """Submit a durable detached Colab job; returns its id immediately (does not block)."""
        spec = JobSpec(
            code=code,
            accelerator=_accelerator(gpu),
            requirements=requirements or [],
            timeout=timeout,
            resumable=resumable,
        )
        info = await self._b().submit(spec)
        return {"id": info.id, "state": info.state.value, "detail": info.detail}

    async def job_status(self, job_id: str) -> dict[str, Any]:
        """Current state of a detached job (PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED)."""
        info = await self._b().status(job_id)
        return {"id": info.id, "state": info.state.value, "detail": info.detail}

    async def job_logs(self, job_id: str, offset: int = 0) -> dict[str, Any]:
        """Incremental logs from ``offset``; pass back the returned offset to continue."""
        text, new_offset = await self._b().log_tail(job_id, offset=offset)
        return {"text": text, "offset": new_offset}

    async def job_result(self, job_id: str) -> dict[str, Any]:
        """Wait for a detached job to finish and return its result."""
        result = await self._b().result(job_id)
        return {
            "id": result.id,
            "state": result.state.value,
            "ok": result.ok,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "error": result.error,
        }

    async def cancel_job(self, job_id: str) -> str:
        """Cancel a running detached job."""
        await self._b().cancel(job_id)
        return f"cancelled {job_id}"

    async def aclose(self) -> None:
        if self._backend is not None:
            await self._backend.aclose()


def build_server(
    client: ColabClient | None = None,
    *,
    transport: str = "cli",
    server_name: str = "colabctl",
    backend_factory: Callable[[str], Backend] = build_backend,
    detached_backend_factory: Callable[[], DetachedColabBackend] = _default_detached_backend,
) -> Any:
    """Build a FastMCP server exposing the colabctl tools (lazy ``mcp`` import)."""
    from mcp.server.fastmcp import FastMCP

    tools = ColabTools(client or ColabClient(transport_name=transport))
    jobs = JobTools(backend_factory)
    detached = DetachedJobTools(detached_backend_factory)
    server = FastMCP(server_name, instructions=SERVER_INSTRUCTIONS)

    # Interactive Colab session tools.
    server.tool()(tools.allocate_runtime)
    server.tool()(tools.run_code)
    server.tool()(tools.list_runtimes)
    server.tool()(tools.runtime_status)
    server.tool()(tools.upload_file)
    server.tool()(tools.download_file)
    server.tool()(tools.interrupt_runtime)
    server.tool()(tools.stop_runtime)
    # Batch-job tools across backends.
    server.tool()(jobs.run_job)
    server.tool()(jobs.list_backends)
    # Durable detached-job tools (submit → poll → collect).
    server.tool()(detached.submit_job)
    server.tool()(detached.job_status)
    server.tool()(detached.job_logs)
    server.tool()(detached.job_result)
    server.tool()(detached.cancel_job)
    return server


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    build_server().run()


if __name__ == "__main__":
    main()
