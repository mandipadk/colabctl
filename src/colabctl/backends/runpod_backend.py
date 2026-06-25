"""RunPod backend — IaaS GPU pods (provision → run → terminate).

Uses the ``runpod`` SDK (``create_pod`` / ``get_pod`` / ``terminate_pod`` — API
confirmed via docs). Auth via ``RUNPOD_API_KEY`` (or ``api_key=``).

Important — RunPod is **infrastructure**, not a managed job platform: you rent a GPU
machine that runs your container. The SDK exposes pod *status* but no clean stdout
capture, so (like the Vertex backend) ``result`` returns the terminal state + a console
pointer — **persist outputs to a RunPod volume or object storage**, don't expect stdout.
This backend is GPU-only. Not live-validated here; mappings + orchestration are
fake-tested. The GPU-type names and status strings are best-effort and may need
adjusting against the live API (override ``gpu_type_id`` / pass your own).
"""

from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Awaitable, Callable
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

#: A GraphQL caller — ``(query, variables) -> data``. Injectable so the spot/bid path is
#: tested without network or the SDK (which has no interruptible/bid parameter).
GraphQL = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

# RunPod's SDK ``create_pod`` cannot set a bid, so spot goes through the legacy GraphQL
# ``podRentInterruptable`` mutation (verified June 2026) with variables (no string-escaping).
_RENT_INTERRUPTABLE = (
    "mutation Rent($input: PodRentInterruptableInput!) {"
    " podRentInterruptable(input: $input) { id desiredStatus } }"
)

# Accelerator → RunPod GPU type id (display names; best-effort).
_RUNPOD_GPU: dict[Accelerator, str] = {
    Accelerator.T4: "NVIDIA T4",
    Accelerator.L4: "NVIDIA L4",
    Accelerator.A100: "NVIDIA A100 80GB PCIe",
    Accelerator.H100: "NVIDIA H100 80GB HBM3",
}
_DEFAULT_IMAGE = "runpod/pytorch:2.1.0-py3.10-cuda11.8.0-devel-ubuntu22.04"
_POLL_INTERVAL = 15.0


def runpod_gpu(accelerator: Accelerator) -> str:
    """Map our accelerator to a RunPod GPU type id (GPU-only backend)."""
    if accelerator is Accelerator.NONE:
        raise ConfigurationError("RunPod backend is GPU-only; pick a GPU accelerator.")
    if accelerator in _RUNPOD_GPU:
        return _RUNPOD_GPU[accelerator]
    raise ConfigurationError(f"RunPod backend has no mapping for {accelerator.value!r}.")


def runpod_state(status: str) -> JobState:
    """Map a RunPod pod status to our :class:`JobState` (best-effort)."""
    s = status.upper()
    if "TERMINAT" in s:
        return JobState.CANCELLED
    if "EXIT" in s or "STOP" in s:  # container finished / pod stopped
        return JobState.SUCCEEDED
    if "RUN" in s:
        return JobState.RUNNING
    return JobState.PENDING


def _build_script(spec: JobSpec) -> str:
    code = spec.resolved_code()
    if spec.requirements:
        reqs = " ".join(shlex.quote(r) for r in spec.requirements)
        return f"pip install -q {reqs} && python -c {shlex.quote(code)}"
    return f"python -c {shlex.quote(code)}"


def _pod_status(pod: Any) -> str:
    if isinstance(pod, dict):
        return str(pod.get("desiredStatus") or pod.get("lastStatusChange") or "")
    return str(getattr(pod, "desiredStatus", "") or "")


@dataclass
class _Job:
    info: JobInfo
    spot: bool = False


def _load_runpod(api_key: str | None) -> Any:
    try:
        import runpod
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise ColabctlError(
            "runpod is not installed. Install with `pip install 'colabctl[runpod]'` "
            "and set RUNPOD_API_KEY."
        ) from exc
    if api_key:
        runpod.api_key = api_key
    return runpod


class RunPodBackend(Backend):
    """Run code on an ephemeral RunPod GPU pod."""

    name = "runpod"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        image: str = _DEFAULT_IMAGE,
        container_disk_gb: int = 20,
        poll_interval: float = _POLL_INTERVAL,
        bid_per_gpu: float | None = None,
        cloud_type: str = "COMMUNITY",
        graphql: GraphQL | None = None,
    ) -> None:
        self._api_key = api_key
        self._image = image
        self._container_disk_gb = container_disk_gb
        self._poll_interval = poll_interval
        self._bid_per_gpu = bid_per_gpu
        self._cloud_type = cloud_type
        self._graphql = graphql or self._default_graphql
        self._jobs: dict[str, _Job] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            name=self.name,
            accelerators=["T4", "L4", "A100", "H100"],
            interactive=False,
            streaming_logs=False,
            persistent=False,
            requires_account=True,
            tos_posture="sanctioned",
            supports_spot=True,
            prepaid_wallet=True,
            preempt_notice_seconds=120,  # RunPod gives ~2 min SIGTERM (not a hard contract)
            notes=[
                "IaaS GPU pods — rents a machine; stdout is NOT captured (persist outputs "
                "to a RunPod volume / object storage). result returns state + console link.",
                "Per-second billing — always terminate; this backend terminates on result().",
                "Spot (--spot) bids via the GraphQL podRentInterruptable mutation (the SDK has "
                "no bid param); needs a max bid (--max-price). Preemption is poll-only — "
                "checkpoint to a volume; an outbid pod can vanish mid-run.",
                "Prepaid wallet — an empty balance blocks/loses pods. Requires RUNPOD_API_KEY.",
            ],
        )

    async def submit(self, spec: JobSpec) -> JobInfo:
        gpu_type = runpod_gpu(spec.accelerator)  # validate early (GPU-only)
        if spec.spot:
            return await self._submit_spot(spec, gpu_type)
        runpod = _load_runpod(self._api_key)
        script = _build_script(spec)

        def _create() -> Any:
            return runpod.create_pod(
                name=spec.name or "colabctl-job",
                image_name=self._image,
                gpu_type_id=gpu_type,
                gpu_count=1,
                container_disk_in_gb=self._container_disk_gb,
                docker_args=f"bash -c {shlex.quote(script)}",
                env=spec.env or None,
            )

        pod = await asyncio.to_thread(_create)
        pod_id = pod["id"] if isinstance(pod, dict) else pod.id
        info = JobInfo(
            id=str(pod_id),
            backend=self.name,
            state=JobState.RUNNING,
            accelerator=spec.accelerator,
            detail=f"https://www.runpod.io/console/pods/{pod_id}",
        )
        self._jobs[info.id] = _Job(info=info)
        return info

    async def _submit_spot(self, spec: JobSpec, gpu_type: str) -> JobInfo:
        """Launch an interruptible (bid-priced) pod via the GraphQL podRentInterruptable path."""
        bid = self._bid_per_gpu if self._bid_per_gpu is not None else spec.max_price_usd_hr
        if bid is None:
            raise ConfigurationError(
                "RunPod spot needs a max bid per GPU-hour: set --max-price (or bid_per_gpu)."
            )
        script = _build_script(spec)
        variables = {
            "input": {
                "bidPerGpu": bid,
                "cloudType": self._cloud_type,
                "gpuTypeId": gpu_type,
                "gpuCount": 1,
                "volumeInGb": 0,
                "containerDiskInGb": self._container_disk_gb,
                "imageName": self._image,
                "dockerArgs": f"bash -c {shlex.quote(script)}",
                "env": [{"key": k, "value": v} for k, v in (spec.env or {}).items()],
            }
        }
        data = await self._graphql(_RENT_INTERRUPTABLE, variables)
        rented = data.get("podRentInterruptable") or {}
        pod_id = rented.get("id")
        if not pod_id:
            # No id ⇒ the bid didn't clear (outbid / no spot capacity). An infra error, so the
            # router fails over to the next candidate (e.g. on-demand) for idempotent jobs.
            raise ColabctlError(
                f"RunPod spot bid (${bid}/GPU-hr) did not clear — no capacity or outbid."
            )
        info = JobInfo(
            id=str(pod_id),
            backend=self.name,
            state=JobState.RUNNING,
            accelerator=spec.accelerator,
            detail=f"spot bid ${bid}/GPU-hr; https://www.runpod.io/console/pods/{pod_id}",
        )
        self._jobs[info.id] = _Job(info=info, spot=True)
        return info

    async def _default_graphql(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:  # pragma: no cover - exercised only against the live API
        import httpx

        key = self._api_key or os.environ.get("RUNPOD_API_KEY")
        if not key:
            raise ColabctlError("RunPod spot needs RUNPOD_API_KEY (GraphQL auth).")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"https://api.runpod.io/graphql?api_key={key}",
                json={"query": query, "variables": variables},
            )
            resp.raise_for_status()
            doc = resp.json()
        if doc.get("errors"):
            raise ColabctlError(f"RunPod GraphQL error: {doc['errors']}")
        data: dict[str, Any] = doc.get("data") or {}
        return data

    async def status(self, job_id: str) -> JobInfo:
        job = self._require(job_id)
        runpod = _load_runpod(self._api_key)
        pod = await asyncio.to_thread(lambda: runpod.get_pod(job_id))
        job.info.state = runpod_state(_pod_status(pod))
        return job.info

    async def logs(self, job_id: str) -> str:
        return (
            f"RunPod does not expose pod stdout via the SDK. View logs in the console: "
            f"https://www.runpod.io/console/pods/{job_id} "
            "(or write outputs to a mounted volume / object storage)."
        )

    async def result(self, job_id: str) -> JobResult:
        try:
            while True:
                info = await self.status(job_id)
                if info.state.is_terminal:
                    break
                await asyncio.sleep(self._poll_interval)
        finally:
            await self._terminate(job_id)  # never leave a pod billing
        return JobResult(
            id=job_id,
            backend=self.name,
            state=info.state,
            stdout="",  # not captured — see logs() / use a volume
            error=None if info.state is JobState.SUCCEEDED else "see RunPod console / volume",
        )

    async def cancel(self, job_id: str) -> None:
        await self._terminate(job_id)
        self._require(job_id).info.state = JobState.CANCELLED

    async def _terminate(self, job_id: str) -> None:
        runpod = _load_runpod(self._api_key)
        await asyncio.to_thread(lambda: runpod.terminate_pod(job_id))

    def _require(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None:
            job = _Job(info=JobInfo(id=job_id, backend=self.name, state=JobState.UNKNOWN))
            self._jobs[job_id] = job
        return job
