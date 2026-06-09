"""Kaggle Notebooks (Kernels) backend — free-GPU fallback.

Pushes a script kernel and runs it (``KaggleApi.kernels_push`` from a folder with a
``kernel-metadata.json``), polls ``kernels_status``, and fetches logs via
``kernels_output`` — API confirmed via docs. Auth is the standard Kaggle credentials
(``~/.kaggle/kaggle.json`` or ``KAGGLE_USERNAME``/``KAGGLE_KEY``). ``kaggle`` is
imported lazily.

Constraints (honest): Kaggle offers only **T4** (free) — no A100/H100/L4 — and has **no
cancel API** (kernels run to completion/timeout). Not live-validated here (no Kaggle
creds); mappings are unit-tested and orchestration is tested against a fake KaggleApi.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
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

# Kaggle's free accelerators are T4 / P100; we expose T4.
_KAGGLE_ACCEL: dict[Accelerator, str] = {Accelerator.T4: "NvidiaTeslaT4"}
_POLL_INTERVAL = 15.0


def kaggle_accelerator(accelerator: Accelerator) -> tuple[bool, str | None]:
    """Map our accelerator to (enable_gpu, Kaggle accelerator name)."""
    if accelerator is Accelerator.NONE:
        return False, None
    if accelerator in _KAGGLE_ACCEL:
        return True, _KAGGLE_ACCEL[accelerator]
    raise ConfigurationError(f"Kaggle only offers T4 GPUs; {accelerator.value!r} is unavailable.")


def kaggle_state(status: str) -> JobState:
    """Map a Kaggle kernel status string to our :class:`JobState`."""
    s = status.lower()
    if "complete" in s:
        return JobState.SUCCEEDED
    if "error" in s:
        return JobState.FAILED
    if "cancel" in s:
        return JobState.CANCELLED
    if "run" in s:
        return JobState.RUNNING
    return JobState.PENDING


def _build_script(spec: JobSpec) -> str:
    code = spec.resolved_code()
    if spec.requirements:
        reqs = ", ".join(repr(r) for r in spec.requirements)
        return (
            "import subprocess, sys\n"
            f"subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', {reqs}], check=True)\n"
            f"{code}"
        )
    return code


def _extract_status(obj: Any) -> str:
    status = getattr(obj, "status", None)
    if status is None and isinstance(obj, dict):
        status = obj.get("status")
    return str(status if status is not None else obj)


@dataclass
class _Job:
    info: JobInfo


def _load_kaggle() -> Any:
    try:
        from kaggle import KaggleApi
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise ColabctlError(
            "kaggle is not installed. Install with `pip install 'colabctl[kaggle]'` "
            "and configure ~/.kaggle/kaggle.json (or KAGGLE_USERNAME/KAGGLE_KEY)."
        ) from exc
    api = KaggleApi()
    api.authenticate()
    return api


class KaggleBackend(Backend):
    """Run batch jobs as Kaggle script kernels."""

    name = "kaggle"

    def __init__(
        self, *, username: str | None = None, poll_interval: float = _POLL_INTERVAL
    ) -> None:
        self._username = username or os.environ.get("KAGGLE_USERNAME")
        self._poll_interval = poll_interval
        self._jobs: dict[str, _Job] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name=self.name,
            accelerators=["T4"],
            interactive=False,
            streaming_logs=False,
            persistent=False,
            requires_account=True,
            tos_posture="sanctioned",
            notes=[
                "Free GPU (T4 only — no A100/H100/L4).",
                "No cancel API; kernels run to completion or the Kaggle timeout.",
                "Needs Kaggle credentials + a username (KAGGLE_USERNAME).",
            ],
        )

    async def submit(self, spec: JobSpec) -> JobInfo:
        if not self._username:
            raise ConfigurationError(
                "Kaggle backend needs a username (constructor or $KAGGLE_USERNAME)."
            )
        enable_gpu, accel = kaggle_accelerator(spec.accelerator)  # validate early
        slug = f"colabctl-{uuid.uuid4().hex[:8]}"
        kernel_id = f"{self._username}/{slug}"
        api = _load_kaggle()
        await asyncio.to_thread(self._push_sync, api, spec, kernel_id, enable_gpu, accel)
        info = JobInfo(
            id=kernel_id,
            backend=self.name,
            state=JobState.RUNNING,
            accelerator=spec.accelerator,
            detail=f"https://www.kaggle.com/code/{kernel_id}",
        )
        self._jobs[kernel_id] = _Job(info=info)
        return info

    async def status(self, job_id: str) -> JobInfo:
        job = self._require(job_id)
        api = _load_kaggle()
        result = await asyncio.to_thread(lambda: api.kernels_status(job_id))
        job.info.state = kaggle_state(_extract_status(result))
        return job.info

    async def logs(self, job_id: str) -> str:
        api = _load_kaggle()

        def _fetch() -> str:
            with tempfile.TemporaryDirectory(prefix="colabctl-kaggle-") as tmp:
                api.kernels_output(job_id, path=tmp)
                parts = [p.read_text(errors="replace") for p in Path(tmp).glob("*.log")]
                return "\n".join(parts)

        return await asyncio.to_thread(_fetch)

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
            error=None if info.state is JobState.SUCCEEDED else "see kernel logs",
        )

    async def cancel(self, job_id: str) -> None:
        raise ColabctlError(
            "Kaggle has no kernel-cancel API; the kernel runs to completion or its timeout."
        )

    def _push_sync(
        self, api: Any, spec: JobSpec, kernel_id: str, enable_gpu: bool, accel: str | None
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="colabctl-kaggle-") as tmp:
            (Path(tmp) / "script.py").write_text(_build_script(spec))
            metadata: dict[str, Any] = {
                "id": kernel_id,
                "title": spec.name or kernel_id.split("/")[-1],
                "code_file": "script.py",
                "language": "python",
                "kernel_type": "script",
                "is_private": "true",
                "enable_gpu": "true" if enable_gpu else "false",
                "enable_internet": "true",
                "dataset_sources": [],
                "competition_sources": [],
                "kernel_sources": [],
            }
            if accel is not None:
                metadata["accelerator"] = accel
            (Path(tmp) / "kernel-metadata.json").write_text(json.dumps(metadata))
            api.kernels_push(folder=tmp)

    def _require(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            job = _Job(info=JobInfo(id=job_id, backend=self.name, state=JobState.UNKNOWN))
            self._jobs[job_id] = job
        return job
