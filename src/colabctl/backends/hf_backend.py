"""Hugging Face Jobs backend — durable, cheap GPU fallback.

Uses ``huggingface_hub`` Jobs (``run_job`` → ``inspect_job`` / ``fetch_job_logs`` /
``cancel_job`` — API confirmed via docs). Unlike the in-process Modal/Colab job model,
HF Jobs are genuinely durable: ``run_job`` returns a remote job id that survives this
process, so submit/status/result/cancel map directly. Auth is the HF token (``HF_TOKEN``
env or ``token=``). ``huggingface_hub`` is imported lazily.

Not live-validated here (no HF token in this env); the flavor/state mappings are
unit-tested and the orchestration is tested against a fake ``huggingface_hub``.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
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

# Accelerator → HF Jobs hardware flavor (https://huggingface.co/docs ... /jobs).
_HF_FLAVOR: dict[Accelerator, str] = {
    Accelerator.T4: "t4-small",
    Accelerator.L4: "l4x1",
    Accelerator.A100: "a100-large",
    Accelerator.H100: "h100x1",
}
_DEFAULT_GPU_IMAGE = "pytorch/pytorch:2.6.0-cuda12.4-cudnn9-devel"
_DEFAULT_CPU_IMAGE = "python:3.12"
_POLL_INTERVAL = 10.0


def hf_flavor(accelerator: Accelerator) -> str:
    """Map our accelerator to an HF Jobs flavor (``cpu-basic`` for CPU)."""
    if accelerator is Accelerator.NONE:
        return "cpu-basic"
    if accelerator in _HF_FLAVOR:
        return _HF_FLAVOR[accelerator]
    raise ConfigurationError(f"HF Jobs does not support accelerator {accelerator.value!r}.")


def hf_state(stage: str) -> JobState:
    """Map an HF job stage string to our :class:`JobState`."""
    s = stage.upper()
    if "COMPLET" in s or "SUCC" in s:
        return JobState.SUCCEEDED
    if "ERROR" in s or "FAIL" in s:
        return JobState.FAILED
    if "CANCEL" in s or "DELET" in s:
        return JobState.CANCELLED
    if "RUN" in s:
        return JobState.RUNNING
    return JobState.PENDING


def _build_command(spec: JobSpec) -> list[str]:
    inner = f"python -c {shlex.quote(spec.resolved_code())}"
    if spec.requirements:
        reqs = " ".join(shlex.quote(r) for r in spec.requirements)
        inner = f"pip install -q {reqs} && {inner}"
    return ["bash", "-lc", inner]


def _extract_stage(info: Any) -> str:
    status = getattr(info, "status", None)
    stage = getattr(status, "stage", None) if status is not None else None
    if stage is None:
        stage = getattr(info, "stage", None) or status or ""
    return str(stage)


@dataclass
class _Job:
    info: JobInfo


def _load_hf() -> Any:
    try:
        import huggingface_hub
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise ColabctlError(
            "huggingface_hub is not installed. Install with `pip install 'colabctl[hf]'` "
            "and set HF_TOKEN."
        ) from exc
    return huggingface_hub


class HFJobsBackend(Backend):
    """Run batch jobs on Hugging Face Jobs."""

    name = "hf"

    def __init__(
        self,
        *,
        token: str | None = None,
        gpu_image: str = _DEFAULT_GPU_IMAGE,
        cpu_image: str = _DEFAULT_CPU_IMAGE,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._token = token
        self._gpu_image = gpu_image
        self._cpu_image = cpu_image
        self._poll_interval = poll_interval
        self._jobs: dict[str, _Job] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name=self.name,
            accelerators=["T4", "L4", "A100", "H100"],
            interactive=False,
            streaming_logs=True,
            persistent=False,
            requires_account=True,
            tos_posture="sanctioned",
            notes=[
                "Durable remote jobs (job id survives the host process).",
                "Requires HF_TOKEN. Pay-per-GPU-second — bound spend via HF's job limits.",
            ],
        )

    async def submit(self, spec: JobSpec) -> JobInfo:
        flavor = hf_flavor(spec.accelerator)  # validate early
        image = self._cpu_image if spec.accelerator is Accelerator.NONE else self._gpu_image
        command = _build_command(spec)
        hf = _load_hf()

        def _run() -> Any:
            return hf.run_job(
                image=image,
                command=command,
                flavor=flavor,
                env=spec.env or None,
                token=self._token,
            )

        job = await asyncio.to_thread(_run)
        info = JobInfo(
            id=str(job.id),
            backend=self.name,
            state=JobState.RUNNING,
            accelerator=spec.accelerator,
            detail=getattr(job, "url", None),
        )
        self._jobs[info.id] = _Job(info=info)
        return info

    async def status(self, job_id: str) -> JobInfo:
        job = self._require(job_id)
        hf = _load_hf()
        info = await asyncio.to_thread(lambda: hf.inspect_job(job_id=job_id))
        job.info.state = hf_state(_extract_stage(info))
        return job.info

    async def logs(self, job_id: str) -> str:
        hf = _load_hf()
        lines = await asyncio.to_thread(lambda: list(hf.fetch_job_logs(job_id=job_id)))
        return "".join(line if line.endswith("\n") else f"{line}\n" for line in lines)

    async def result(self, job_id: str) -> JobResult:
        while True:
            info = await self.status(job_id)
            if info.state.is_terminal:
                break
            await asyncio.sleep(self._poll_interval)
        stdout = await self.logs(job_id)
        return JobResult(
            id=job_id,
            backend=self.name,
            state=info.state,
            stdout=stdout,
            error=None if info.state is JobState.SUCCEEDED else "see job logs",
        )

    async def cancel(self, job_id: str) -> None:
        hf = _load_hf()
        await asyncio.to_thread(lambda: hf.cancel_job(job_id=job_id))
        self._require(job_id).info.state = JobState.CANCELLED

    def _require(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            # HF jobs are durable; allow status/logs/cancel on ids we didn't submit.
            job = _Job(info=JobInfo(id=job_id, backend=self.name, state=JobState.UNKNOWN))
            self._jobs[job_id] = job
        return job
