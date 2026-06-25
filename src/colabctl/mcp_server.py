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

import functools
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from colabctl.backends.base import Backend, JobSpec, JobState
from colabctl.backends.factory import BACKEND_NAMES, build_backend, build_router
from colabctl.backends.router import BackendRouter
from colabctl.errors import ColabctlError, ConfigurationError
from colabctl.models import Accelerator, ExecutionResult, SessionInfo
from colabctl.sdk.client import ColabClient

if TYPE_CHECKING:
    from colabctl.jobs.backend import DetachedColabBackend

SERVER_INSTRUCTIONS = (
    "Drive Google Colab: allocate GPU runtimes, run code, move files. "
    "Allocate once, reuse the returned session name across run_code calls, and "
    "stop_runtime when done. Runtimes are ephemeral — persist important artifacts "
    "with download_file. Prefer T4 for cheap work; A100/H100 may be unavailable. "
    "For long-running work, prefer the detached job tools — they follow the MCP Tasks "
    "model: submit_job returns a taskId (= the job id) with a status "
    "(working/completed/failed/cancelled); poll job_status until status is terminal, then "
    "collect job_result. The job is DURABLE: it survives across calls, disconnects, and even "
    "this server, auto-resuming from its checkpoint if the runtime is reclaimed — so do other "
    "work between polls instead of blocking on run_job. Errors carry a stable `code`, "
    "`category`, and a `remediation` hint."
)

#: JobState → MCP Tasks (SEP-1686) status vocabulary, so agents recognize a detached job as a
#: durable task. We map to the spec's five values; ``input_required`` never applies to us.
_TASK_STATUS: dict[JobState, str] = {
    JobState.PENDING: "working",
    JobState.RUNNING: "working",
    JobState.SUCCEEDED: "completed",
    JobState.FAILED: "failed",
    JobState.CANCELLED: "cancelled",
    JobState.UNKNOWN: "working",
}


def _task_fields(job_id: str, state: JobState) -> dict[str, str]:
    """The Tasks-shaped fields (taskId + spec status) merged into detached-job responses."""
    return {"taskId": job_id, "status": _TASK_STATUS.get(state, "working")}


async def health_check() -> dict[str, Any]:
    """colabctl preflight health (auth, colab binary, backends, state, agent skill).

    Call this when a runtime/job won't start to find out why (e.g. missing credentials or the
    google-colab-cli binary) before retrying. Each check has a status (ok/warn/fail) + a fix.
    """
    from colabctl.doctor import overall_status, run_checks

    checks = run_checks()
    return {"status": overall_status(checks), "checks": [c.to_dict() for c in checks]}


def _coded(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap a tool so a raised ``ColabctlError`` carries its stable code + remediation, instead
    of a bare human string — agents can branch on the code/category and act on the hint."""

    @functools.wraps(fn)
    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ColabctlError as exc:
            d = exc.to_dict()
            msg = f"{d['message']} [code={d['code']} category={d['category']}]"
            if d.get("remediation"):
                msg += f" remediation: {d['remediation']}"
            raise ColabctlError(msg) from exc

    return _wrapped


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

    async def run_once(
        self, code: str, gpu: str = "T4", timeout: float | None = None
    ) -> dict[str, Any]:
        """Allocate a runtime, run ``code``, and tear it down — one call, no session to manage.

        Collapses the allocate_runtime → run_code → stop_runtime dance for quick one-shot work.
        For long-running work prefer ``submit_job`` (durable, survives disconnects); for several
        runs on one GPU, ``allocate_runtime`` once and reuse ``run_code``.
        """
        session = await self._client.allocate(gpu=gpu, keep=False)
        try:
            return _result_dict(await session.run(code, timeout=timeout))
        finally:
            await session.stop()  # always release the one-shot runtime

    async def run_file(
        self, path: str, gpu: str = "T4", timeout: float | None = None
    ) -> dict[str, Any]:
        """Run a local ``.py`` file one-shot on a fresh runtime, then tear it down."""
        from pathlib import Path

        return await self.run_once(Path(path).read_text(), gpu=gpu, timeout=timeout)

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

    def __init__(
        self,
        backend_factory: Callable[[str], Backend] = build_backend,
        router_factory: Callable[[list[str]], BackendRouter] = build_router,
    ) -> None:
        self._factory = backend_factory
        self._router_factory = router_factory
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
        allow: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run Python ``code`` on a backend (colab/modal/vertex) and return the result.

        Pass ``allow`` (e.g. ``["colab", "modal"]``) to fail over across backends on infra
        errors — ``backend`` is preferred first. The job is re-run on the next backend if
        one fails to allocate, so use ``allow`` only for idempotent work.
        """
        spec = JobSpec(
            code=code,
            accelerator=_accelerator(gpu),
            requirements=requirements or [],
            timeout=timeout,
        )
        if allow:
            router = self._router_factory(allow)
            try:
                result = await router.run(spec, prefer=backend, fallback=True)
            finally:
                await router.aclose()
        else:
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

    async def run_notebook(
        self,
        path: str,
        backend: str = "colab",
        gpu: str = "T4",
        parameters: dict[str, Any] | None = None,
        requirements: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a parameterized ``.ipynb`` (by local path) as a job on a backend."""
        from colabctl.notebook import run_notebook_job

        result = await run_notebook_job(
            self._backend(backend),
            path,
            parameters=parameters,
            accelerator=_accelerator(gpu),
            requirements=requirements or [],
        )
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
        track: str | None = None,
    ) -> dict[str, Any]:
        """Submit a durable detached Colab job; returns its id immediately (does not block).

        ``track="wandb"|"mlflow"`` enables experiment tracking (creds from the secret store; the
        run is tagged with the job id and its URL is captured into the audit ledger).
        """
        spec = JobSpec(
            code=code,
            accelerator=_accelerator(gpu),
            requirements=requirements or [],
            timeout=timeout,
            resumable=resumable,
            track=track,
        )
        info = await self._b().submit(spec)
        return {
            "id": info.id,
            "state": info.state.value,
            "detail": info.detail,
            **_task_fields(info.id, info.state),
        }

    async def job_status(self, job_id: str) -> dict[str, Any]:
        """Detached-job state, Tasks-shaped (status: working/completed/failed/cancelled)."""
        info = await self._b().status(job_id)
        return {
            "id": info.id,
            "state": info.state.value,
            "detail": info.detail,
            **_task_fields(info.id, info.state),
        }

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
            **_task_fields(result.id, result.state),
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

    def _register(*fns: Callable[..., Awaitable[Any]]) -> None:
        # Every tool is wrapped so a raised ColabctlError surfaces its stable code + remediation.
        for fn in fns:
            server.tool()(_coded(fn))

    # Interactive Colab session tools (+ one-shot allocate→run→teardown tools).
    _register(
        tools.allocate_runtime,
        tools.run_code,
        tools.run_once,
        tools.run_file,
        tools.list_runtimes,
        tools.runtime_status,
        tools.upload_file,
        tools.download_file,
        tools.interrupt_runtime,
        tools.stop_runtime,
    )
    # Batch-job tools across backends.
    _register(jobs.run_job, jobs.run_notebook, jobs.list_backends)
    # Preflight health (why won't a runtime/job start).
    _register(health_check)
    # Durable detached-job tools (submit → poll → collect; MCP Tasks-shaped).
    _register(
        detached.submit_job,
        detached.job_status,
        detached.job_logs,
        detached.job_result,
        detached.cancel_job,
    )
    return server


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    build_server().run()


if __name__ == "__main__":
    main()
