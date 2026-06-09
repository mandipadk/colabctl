"""Vertex AI backend — sanctioned, headless, deadline-bound GPU jobs.

Submits a Vertex AI ``CustomJob`` from a local script
(``aiplatform.CustomJob.from_local_script(...).submit()`` — API confirmed against
the current SDK), polls ``job.state``, and cancels via ``job.cancel()``. Auth is ADC
(or a service account); project / location / staging bucket come from the constructor
or ``GOOGLE_CLOUD_PROJECT`` / ``VERTEX_LOCATION`` / ``VERTEX_STAGING_BUCKET``.
``google-cloud-aiplatform`` is imported lazily.

Limitations (honest, v1): stdout is *not* captured — Vertex job output goes to Cloud
Logging, so :meth:`result` returns the terminal state + a log pointer rather than
program output, and artifacts are expected in GCS. Not live-validated here (no GCP
project); the accelerator/state mappings are unit-tested and the SDK calls follow docs.
"""

from __future__ import annotations

import asyncio
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

# Accelerator → (Vertex accelerator_type, a compatible machine_type).
_VERTEX_ACCEL: dict[Accelerator, tuple[str, str]] = {
    Accelerator.T4: ("NVIDIA_TESLA_T4", "n1-standard-4"),
    Accelerator.L4: ("NVIDIA_L4", "g2-standard-4"),
    Accelerator.A100: ("NVIDIA_TESLA_A100", "a2-highgpu-1g"),
    Accelerator.H100: ("NVIDIA_H100_80GB", "a3-highgpu-1g"),
}
_DEFAULT_CPU_MACHINE = "n1-standard-4"
_DEFAULT_CONTAINER = "us-docker.pkg.dev/vertex-ai/training/pytorch-gpu.2-4.py311:latest"
_DEFAULT_LOCATION = "us-central1"
_POLL_INTERVAL = 15.0


def vertex_accelerator(accelerator: Accelerator) -> tuple[str | None, str]:
    """Map our accelerator to (Vertex ``accelerator_type`` or None, ``machine_type``)."""
    if accelerator is Accelerator.NONE:
        return None, _DEFAULT_CPU_MACHINE
    if accelerator in _VERTEX_ACCEL:
        return _VERTEX_ACCEL[accelerator]
    raise ConfigurationError(f"Vertex backend does not support accelerator {accelerator.value!r}.")


def vertex_state(raw: str) -> JobState:
    """Map a Vertex ``JobState`` (e.g. ``JOB_STATE_SUCCEEDED``) to our :class:`JobState`."""
    name = raw.upper()
    if "SUCCEEDED" in name:
        return JobState.SUCCEEDED
    if "FAILED" in name:
        return JobState.FAILED
    if "CANCEL" in name:  # CANCELLED / CANCELLING
        return JobState.CANCELLED
    if "RUNNING" in name:
        return JobState.RUNNING
    if "PENDING" in name or "QUEUED" in name:
        return JobState.PENDING
    return JobState.UNKNOWN


@dataclass
class _Job:
    info: JobInfo
    resource_name: str


def _load_aiplatform() -> Any:
    try:
        from google.cloud import aiplatform
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise ColabctlError(
            "google-cloud-aiplatform is not installed. Install with "
            "`pip install 'colabctl[vertex]'`."
        ) from exc
    return aiplatform


class VertexBackend(Backend):
    """Run batch jobs as Vertex AI CustomJobs."""

    name = "vertex"

    def __init__(
        self,
        *,
        project: str | None = None,
        location: str | None = None,
        staging_bucket: str | None = None,
        container_uri: str = _DEFAULT_CONTAINER,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        self._location = location or os.environ.get("VERTEX_LOCATION") or _DEFAULT_LOCATION
        self._staging_bucket = staging_bucket or os.environ.get("VERTEX_STAGING_BUCKET")
        self._container_uri = container_uri
        self._poll_interval = poll_interval
        self._jobs: dict[str, _Job] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name=self.name,
            accelerators=[t for t, _ in _VERTEX_ACCEL.values()] + ["T4", "L4", "A100", "H100"],
            interactive=False,
            streaming_logs=False,
            persistent=False,
            requires_account=True,
            tos_posture="sanctioned",
            notes=[
                "Fully sanctioned, headless, deadline-bound GPU jobs.",
                "Needs a GCP project + staging bucket; stdout goes to Cloud Logging "
                "(result returns state + a log pointer, not program output).",
            ],
        )

    def _require_config(self) -> tuple[str, str, str]:
        if not self._project:
            raise ConfigurationError(
                "Vertex backend needs a project (constructor or $GOOGLE_CLOUD_PROJECT)."
            )
        if not self._staging_bucket:
            raise ConfigurationError(
                "Vertex backend needs a staging bucket (constructor or $VERTEX_STAGING_BUCKET)."
            )
        return self._project, self._location, self._staging_bucket

    async def submit(self, spec: JobSpec) -> JobInfo:
        vertex_accelerator(spec.accelerator)  # validate early
        self._require_config()
        job_id = f"vertex-{uuid.uuid4().hex[:10]}"
        resource_name = await asyncio.to_thread(self._submit_sync, spec, job_id)
        info = JobInfo(
            id=job_id,
            backend=self.name,
            state=JobState.PENDING,
            accelerator=spec.accelerator,
            detail=resource_name,
        )
        self._jobs[job_id] = _Job(info=info, resource_name=resource_name)
        return info

    async def status(self, job_id: str) -> JobInfo:
        job = self._require(job_id)
        raw = await asyncio.to_thread(self._state_sync, job.resource_name)
        job.info.state = vertex_state(raw)
        return job.info

    async def logs(self, job_id: str) -> str:
        job = self._require(job_id)
        project, location, _ = self._require_config()
        return (
            f"Vertex CustomJob logs are in Cloud Logging. View: "
            f"https://console.cloud.google.com/vertex-ai/locations/{location}/training/"
            f"{job.resource_name.rsplit('/', 1)[-1]}?project={project}"
        )

    async def result(self, job_id: str) -> JobResult:
        job = self._require(job_id)
        while True:
            info = await self.status(job_id)
            if info.state.is_terminal:
                break
            await asyncio.sleep(self._poll_interval)
        return JobResult(
            id=job_id,
            backend=self.name,
            state=job.info.state,
            error=None if job.info.state is JobState.SUCCEEDED else "see Cloud Logging",
            stdout="",  # Vertex stdout is in Cloud Logging, not captured here
        )

    async def cancel(self, job_id: str) -> None:
        job = self._require(job_id)
        await asyncio.to_thread(self._cancel_sync, job.resource_name)
        job.info.state = JobState.CANCELLED

    # -- internals ----------------------------------------------------------

    def _require(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise ColabctlError(f"No such job: {job_id!r}")
        return job

    def _submit_sync(self, spec: JobSpec, job_id: str) -> str:
        aiplatform = _load_aiplatform()
        project, location, staging_bucket = self._require_config()
        aiplatform.init(project=project, location=location, staging_bucket=staging_bucket)
        accel_type, machine_type = vertex_accelerator(spec.accelerator)

        tmpdir: str | None = None
        script_path = spec.script_path
        if script_path is None:
            tmpdir = tempfile.mkdtemp(prefix="colabctl-vertex-")
            script_path = str(Path(tmpdir) / "job.py")
            Path(script_path).write_text(spec.resolved_code())
        try:
            kwargs: dict[str, Any] = {
                "display_name": spec.name or job_id,
                "script_path": script_path,
                "container_uri": self._container_uri,
                "requirements": spec.requirements,
                "machine_type": machine_type,
            }
            if accel_type is not None:
                kwargs["accelerator_type"] = accel_type
                kwargs["accelerator_count"] = 1
            job = aiplatform.CustomJob.from_local_script(**kwargs)
            job.submit()  # non-blocking
            return str(job.resource_name)
        finally:
            if tmpdir is not None:
                Path(script_path).unlink(missing_ok=True)
                Path(tmpdir).rmdir()

    def _state_sync(self, resource_name: str) -> str:
        aiplatform = _load_aiplatform()
        job = aiplatform.CustomJob.get(resource_name)
        return str(job.state)

    def _cancel_sync(self, resource_name: str) -> None:
        aiplatform = _load_aiplatform()
        aiplatform.CustomJob.get(resource_name).cancel()
