"""Colab as a batch :class:`Backend` (wraps a TransportAdapter).

Presents Colab's interactive transport through the unified job API: ``submit``
launches an in-process asyncio task that allocates a runtime, optionally pip-installs
requirements, runs the code, captures output, and releases the runtime. State/logs/
result read the in-memory job record.

Limitation: job tracking is in-process (if the host process dies, the record is lost).
Cross-process durability is the runtime-lifecycle manager's concern (checkpoint to
Drive/GCS); for the interactive, warm-GPU workflow use the SDK's ``ColabSession`` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field

from colabctl.backends.base import (
    Backend,
    BackendCapabilities,
    JobInfo,
    JobResult,
    JobSpec,
    JobState,
)
from colabctl.errors import ColabctlError
from colabctl.models import RuntimeSpec
from colabctl.transport.base import TransportAdapter


def _install_code(requirements: list[str]) -> str:
    pkgs = ", ".join(repr(r) for r in requirements)
    return (
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', {pkgs}], check=True)\n"
    )


@dataclass
class _Job:
    info: JobInfo
    spec: JobSpec
    task: asyncio.Task[None] | None = None
    session: str | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None
    logbuf: list[str] = field(default_factory=list)


class ColabBackend(Backend):
    """Run batch jobs on Colab via an interactive transport."""

    name = "colab"

    def __init__(self, transport: TransportAdapter) -> None:
        self._transport = transport
        self._jobs: dict[str, _Job] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        caps = self._transport.capabilities
        return BackendCapabilities(
            name=self.name,
            accelerators=["T4", "L4", "G4", "A100", "H100"],
            interactive=caps.interactive,
            streaming_logs=False,
            # Honest until the detached-job manager lands (plan Pillar 2): job records
            # are in-process today, so nothing about a job survives this process.
            persistent=False,
            requires_account=True,
            tos_posture="sanctioned" if self._transport.name == "cli" else "gray-area",
            notes=[
                f"via the {self._transport.name!r} transport",
                "Job records are in-process: they do not survive the submitting process "
                "(durable detached jobs are planned — docs/plan.md Pillar 2).",
                *caps.caveats,
            ],
        )

    async def submit(self, spec: JobSpec) -> JobInfo:
        job_id = f"colab-{uuid.uuid4().hex[:10]}"
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
        await self._transport.aclose()

    # -- internals ----------------------------------------------------------

    def _require(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ColabctlError(f"No such job: {job_id!r}")
        return job

    async def _execute(self, job: _Job) -> None:
        job.info.state = JobState.RUNNING
        try:
            session = await self._transport.allocate(
                RuntimeSpec(accelerator=job.spec.accelerator, name=job.spec.name)
            )
            job.session = session.name
            if job.spec.requirements:
                install = await self._transport.execute(
                    session.name, _install_code(job.spec.requirements), timeout=job.spec.timeout
                )
                if not install.ok:
                    job.error = "pip install failed: " + (install.stderr or install.text)[:400]
                    job.info.state = JobState.FAILED
                    return
            result = await self._transport.execute(
                session.name, job.spec.resolved_code(), timeout=job.spec.timeout
            )
            job.stdout = result.text
            job.stderr = result.stderr
            job.logbuf.append(result.text)
            if result.ok:
                job.info.state = JobState.SUCCEEDED
                job.exit_code = 0
            else:
                job.info.state = JobState.FAILED
                job.exit_code = 1
                if result.error is not None:
                    job.error = f"{result.error.ename}: {result.error.evalue}"
        except asyncio.CancelledError:
            job.info.state = JobState.CANCELLED
            raise
        except ColabctlError as exc:
            job.info.state = JobState.FAILED
            job.error = str(exc)
        finally:
            if job.session is not None:
                with contextlib.suppress(ColabctlError):
                    await self._transport.stop(job.session)
