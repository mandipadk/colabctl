"""Modal backend — gVisor-isolated GPU sandboxes (verdict score 8, ideal for agent code).

Implemented against the documented Modal Sandbox async API (``modal.Sandbox.create.aio``
→ ``exec.aio`` → ``stdout.read.aio`` / ``wait.aio`` / ``returncode`` → ``terminate.aio``).
Auth is via the ``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET`` env vars (read by the modal
client). ``modal`` is imported lazily.

Not yet live-validated in this environment (no Modal account); the accelerator mapping
and state handling are unit-tested, and the SDK calls follow current docs.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from colabctl.backends.base import (
    Backend,
    BackendCapabilities,
    JobInfo,
    JobResult,
    JobSpec,
    JobState,
)
from colabctl.errors import ColabctlError, ConfigurationError
from colabctl.models import Accelerator
from colabctl.observability import cap_timeout

# Accelerator → Modal GPU string (https://modal.com/docs/guide/gpu).
_MODAL_GPU = {
    Accelerator.T4: "T4",
    Accelerator.L4: "L4",
    Accelerator.A100: "A100",
    Accelerator.H100: "H100",
}


def modal_gpu(accelerator: Accelerator) -> str | None:
    """Map our accelerator to a Modal GPU string (``None`` for CPU)."""
    if accelerator is Accelerator.NONE:
        return None
    if accelerator in _MODAL_GPU:
        return _MODAL_GPU[accelerator]
    raise ConfigurationError(f"Modal does not support accelerator {accelerator.value!r}.")


@dataclass
class _Job:
    info: JobInfo
    spec: JobSpec
    task: asyncio.Task[None] | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None
    logbuf: list[str] = field(default_factory=list)


def _load_modal() -> Any:
    try:
        import modal
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise ColabctlError(
            "modal is not installed. Install with `pip install 'colabctl[modal]'` "
            "and set MODAL_TOKEN_ID / MODAL_TOKEN_SECRET."
        ) from exc
    return modal


class ModalBackend(Backend):
    """Run batch jobs in Modal Sandboxes."""

    name = "modal"

    def __init__(
        self,
        *,
        app_name: str = "colabctl",
        python_version: str = "3.12",
        default_timeout: int = 600,
        max_timeout: int = 3600,
    ) -> None:
        self._app_name = app_name
        self._python_version = python_version
        self._default_timeout = default_timeout
        self._max_timeout = max_timeout  # spend guard: hard ceiling on billable time
        self._jobs: dict[str, _Job] = {}

    def _effective_timeout(self, spec: JobSpec) -> int:
        return cap_timeout(
            spec.timeout or self._default_timeout, maximum=self._max_timeout, label="modal"
        )

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name=self.name,
            accelerators=list(_MODAL_GPU.values()),
            interactive=False,
            streaming_logs=True,
            persistent=False,
            max_runtime_seconds=None,
            requires_account=True,
            tos_posture="sanctioned",
            notes=[
                "gVisor-isolated sandboxes; ideal for untrusted/agent-generated code.",
                "Requires MODAL_TOKEN_ID / MODAL_TOKEN_SECRET. Pay-per-GPU-second — "
                "enforce timeouts to bound spend.",
            ],
        )

    async def submit(self, spec: JobSpec) -> JobInfo:
        modal_gpu(spec.accelerator)  # validate early
        job_id = f"modal-{uuid.uuid4().hex[:10]}"
        info = JobInfo(
            id=job_id, backend=self.name, state=JobState.PENDING, accelerator=spec.accelerator
        )
        job = _Job(info=info, spec=spec)
        self._jobs[job_id] = job
        job.task = asyncio.create_task(self._execute(job))
        return info

    async def status(self, job_id: str) -> JobInfo:
        return self._require(job_id).info

    async def logs(self, job_id: str) -> str:
        return "".join(self._require(job_id).logbuf)

    async def result(self, job_id: str) -> JobResult:
        job = self._require(job_id)
        if job.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await job.task
        return JobResult(
            id=job_id,
            backend=self.name,
            state=job.info.state,
            exit_code=job.exit_code,
            stdout=job.stdout,
            stderr=job.stderr,
            error=job.error,
        )

    async def cancel(self, job_id: str) -> None:
        job = self._require(job_id)
        if job.task is not None and not job.task.done():
            job.task.cancel()
        job.info.state = JobState.CANCELLED

    async def aclose(self) -> None:
        for job in list(self._jobs.values()):
            if job.task is not None and not job.task.done():
                job.task.cancel()

    # -- internals ----------------------------------------------------------

    def _require(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ColabctlError(f"No such job: {job_id!r}")
        return job

    async def _execute(self, job: _Job) -> None:
        job.info.state = JobState.RUNNING
        modal = _load_modal()
        gpu = modal_gpu(job.spec.accelerator)
        timeout = self._effective_timeout(job.spec)
        sandbox = None
        try:
            image = modal.Image.debian_slim(python_version=self._python_version)
            if job.spec.requirements:
                image = image.pip_install(*job.spec.requirements)
            app = await modal.App.lookup.aio(self._app_name, create_if_missing=True)
            create_kwargs: dict[str, Any] = {"image": image, "app": app, "timeout": timeout}
            if gpu is not None:
                create_kwargs["gpu"] = gpu
            sandbox = await modal.Sandbox.create.aio(**create_kwargs)
            job.info.detail = getattr(sandbox, "object_id", None)

            proc = await sandbox.exec.aio("python", "-c", job.spec.resolved_code())
            job.stdout = await proc.stdout.read.aio()
            job.stderr = await proc.stderr.read.aio()
            await proc.wait.aio()
            job.exit_code = proc.returncode
            job.logbuf.append(job.stdout)
            job.info.state = JobState.SUCCEEDED if proc.returncode == 0 else JobState.FAILED
            if proc.returncode != 0:
                job.error = (job.stderr or "")[:400] or f"exit code {proc.returncode}"
        except asyncio.CancelledError:
            job.info.state = JobState.CANCELLED
            raise
        except Exception as exc:
            job.info.state = JobState.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            if sandbox is not None:
                with contextlib.suppress(Exception):  # best-effort teardown
                    await sandbox.terminate.aio(wait=True)
