"""Provider abstraction: the batch-`Backend` contract.

Two complementary abstractions exist in colabctl:

- :class:`~colabctl.transport.base.TransportAdapter` — *interactive* runtimes
  (allocate a warm GPU, run cells on a live kernel). Colab's native shape.
- :class:`Backend` (this module) — *batch jobs* (submit code → poll → fetch result →
  cancel). The natural shape for Modal, Vertex, HF Jobs, etc.

The :class:`~colabctl.backends.router.BackendRouter` selects a backend by capability
and fails over between them, so a Colab outage/quota/ban degrades to Modal or Vertex
instead of failing. Colab is also exposed as a batch backend
(:class:`~colabctl.backends.colab.ColabBackend`) so callers can use one job API
across every provider.
"""

from __future__ import annotations

import abc
import enum

from pydantic import BaseModel, model_validator

from colabctl.models import Accelerator


class JobState(enum.StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


class JobSpec(BaseModel):
    """What to run on a backend. Provide exactly one of ``code`` or ``script_path``."""

    code: str | None = None
    script_path: str | None = None
    requirements: list[str] = []
    accelerator: Accelerator = Accelerator.T4
    env: dict[str, str] = {}
    timeout: int | None = None
    name: str | None = None
    #: Prefer the interruptible/spot tier where a backend offers one (cheaper, preemptible).
    spot: bool = False
    #: Per-job fail-closed price ceiling — refuse to launch on any backend pricier than this
    #: ``$/hr`` (a budget *guarantee*, not a preference; OpenRouter ``max_price`` semantics).
    max_price_usd_hr: float | None = None
    #: Experiment tracking: ``"wandb"`` or ``"mlflow"`` to inject creds (from the secret store),
    #: enable autolog, tag the run with the job id, and capture the run URL into the audit ledger.
    track: str | None = None
    #: Whether the workload resumes idempotently from its own checkpoint after a
    #: runtime re-assign — the opt-in that lets the lifecycle manager auto-resume a
    #: detached job on reclamation (plan Pillar 2) rather than failing it.
    resumable: bool = False

    @model_validator(mode="after")
    def _exactly_one_source(self) -> JobSpec:
        if bool(self.code) == bool(self.script_path):
            raise ValueError("Provide exactly one of `code` or `script_path`.")
        return self

    def resolved_code(self) -> str:
        """Return the code to run (reads ``script_path`` if that's what was given)."""
        if self.code is not None:
            return self.code
        from pathlib import Path

        return Path(self.script_path or "").read_text()


class JobInfo(BaseModel):
    """A lightweight view of a submitted job."""

    id: str
    backend: str
    state: JobState
    accelerator: Accelerator = Accelerator.NONE
    detail: str | None = None


class JobResult(BaseModel):
    """The outcome of a job."""

    id: str
    backend: str
    state: JobState
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is JobState.SUCCEEDED


class BackendCapabilities(BaseModel):
    """What a backend can do — used for routing and honest disclosure."""

    name: str
    accelerators: list[str] = []  # supported accelerator values; empty = any/unknown
    interactive: bool = False
    streaming_logs: bool = False
    persistent: bool = False  # runtime survives between calls
    max_runtime_seconds: int | None = None
    requires_account: bool = True
    tos_posture: str = "sanctioned"  # "sanctioned" | "gray-area" | "prohibited"
    #: Cost-engine flags (Phase 2). ``supports_spot``: offers an interruptible tier.
    #: ``prepaid_wallet``: spend is gated by a prepaid balance (Vast/RunPod).
    #: ``preempt_notice_seconds``: graceful-drain window before a spot preemption
    #: (0 = none — the client must checkpoint frequently).
    supports_spot: bool = False
    prepaid_wallet: bool = False
    preempt_notice_seconds: int | None = None
    notes: list[str] = []

    def supports(self, accelerator: Accelerator) -> bool:
        if not self.accelerators:
            return True
        return accelerator.value in self.accelerators or accelerator is Accelerator.NONE


class Backend(abc.ABC):
    """A pluggable batch-execution backend (Colab, Modal, Vertex, ...)."""

    name: str = "backend"

    @property
    @abc.abstractmethod
    def capabilities(self) -> BackendCapabilities: ...

    @abc.abstractmethod
    async def submit(self, spec: JobSpec) -> JobInfo:
        """Start a job and return immediately with its handle."""

    @abc.abstractmethod
    async def status(self, job_id: str) -> JobInfo:
        """Return the job's current state."""

    @abc.abstractmethod
    async def logs(self, job_id: str) -> str:
        """Return the job's logs so far (best-effort)."""

    @abc.abstractmethod
    async def result(self, job_id: str) -> JobResult:
        """Wait for the job to finish and return its result."""

    @abc.abstractmethod
    async def cancel(self, job_id: str) -> None:
        """Cancel a running job."""

    async def run(self, spec: JobSpec) -> JobResult:
        """Convenience: submit and wait for the result."""
        info = await self.submit(spec)
        return await self.result(info.id)

    async def aclose(self) -> None:
        """Release backend-level resources (default no-op; override if needed)."""
        return None
