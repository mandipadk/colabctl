"""``DetachedColabBackend`` — durable, cross-process Colab jobs (Pillar 2).

The durable counterpart to the synchronous :class:`~colabctl.backends.colab.ColabBackend`:
``submit`` allocates a runtime, launches the work as a **detached supervised process**
(see :mod:`colabctl.jobs`), persists a :class:`~colabctl.state.StoredJob`, and returns —
leaving the runtime running. ``status``/``logs``/``result``/``cancel`` then work from
**any process** by reading the record and reattaching the session (native transport,
Pillar 1). That is the "submit → close the laptop → collect later" promise.

It deliberately does **not** release the runtime on completion: the job's logs live on
the VM, and the runtime is a reusable resource. Teardown is the caller's explicit act
(``colabctl stop`` / ``gc``). The synchronous ``ColabBackend`` (allocate→run→release) is
kept untouched for the fire-and-forget ``run_job`` path.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from colabctl.backends.base import (
    Backend,
    BackendCapabilities,
    JobInfo,
    JobResult,
    JobSpec,
    JobState,
)
from colabctl.errors import ColabctlError, JobError, RuntimeUnavailableError
from colabctl.jobs.codes import DEFAULT_JOBS_ROOT
from colabctl.jobs.runtime import KernelJobRuntime, job_state_from
from colabctl.models import RuntimeSpec
from colabctl.observability import get_logger
from colabctl.state import StateStore, StoredJob
from colabctl.transport.base import TransportAdapter

_log = get_logger("jobs.backend")
_LOG_CHUNK = 65536


class DetachedColabBackend(Backend):
    """Submit detached jobs to Colab and manage them durably across processes."""

    name = "colab"

    def __init__(
        self,
        transport: TransportAdapter,
        *,
        state: StateStore | None = None,
        root: str = DEFAULT_JOBS_ROOT,
        poll_interval: float = 2.0,
    ) -> None:
        self._transport = transport
        self._state = state if state is not None else StateStore()
        self._runtime = KernelJobRuntime(transport, root=root)
        self._poll_interval = poll_interval

    @classmethod
    def create(
        cls,
        *,
        auth_mode: str = "adc",
        allow_native: bool = False,
        state: StateStore | None = None,
        root: str = DEFAULT_JOBS_ROOT,
        poll_interval: float = 2.0,
    ) -> DetachedColabBackend:
        """Build over the native transport (the only one that can attach cross-process).

        The state store is shared between the transport (session records) and this
        backend (job records) so both halves of a reattach resolve from one file.
        """
        from colabctl.auth import ADCAuthProvider
        from colabctl.transport.native import NativeColabTransport

        shared = state if state is not None else StateStore()
        transport = NativeColabTransport.create(
            ADCAuthProvider(), allow_native=allow_native, state=shared
        )
        return cls(transport, state=shared, root=root, poll_interval=poll_interval)

    @property
    def capabilities(self) -> BackendCapabilities:
        caps = self._transport.capabilities
        return BackendCapabilities(
            name=self.name,
            accelerators=["T4", "L4", "G4", "A100", "H100"],
            interactive=caps.interactive,
            streaming_logs=True,  # logs spool on the VM; we tail them by offset
            persistent=True,  # jobs survive the submitting process (state store)
            requires_account=True,
            tos_posture="sanctioned" if self._transport.name == "cli" else "gray-area",
            notes=[
                f"detached jobs via the {self._transport.name!r} transport",
                "The runtime is left running after a job completes (its logs live on "
                "it); release it with `colabctl stop` / `gc` when done.",
                *caps.caveats,
            ],
        )

    # -- Backend contract ---------------------------------------------------

    async def submit(self, spec: JobSpec) -> JobInfo:
        job_id = f"colab-{uuid.uuid4().hex[:10]}"
        session = await self._transport.allocate(
            RuntimeSpec(accelerator=spec.accelerator, name=spec.name)
        )
        launched = await self._runtime.launch(
            session.name,
            job_id,
            script=spec.resolved_code(),
            requirements=spec.requirements,
            timeout=spec.timeout,
        )
        self._state.put_job(
            StoredJob(
                id=job_id,
                session_name=session.name,
                backend=self.name,
                state=JobState.RUNNING,
                accelerator=spec.accelerator,
                requirements=list(spec.requirements),
                code=spec.resolved_code(),
                timeout=spec.timeout,
                resumable=spec.resumable,
                remote_dir=launched.remote_dir,
                pid=launched.pid,
            )
        )
        return JobInfo(
            id=job_id,
            backend=self.name,
            state=JobState.RUNNING,
            accelerator=spec.accelerator,
            detail=f"detached on {session.name}",
        )

    async def status(self, job_id: str) -> JobInfo:
        job = self._require(job_id)
        snapshot = await self._poll_or_resume(job)
        state = job_state_from(snapshot)
        self._persist_state(job, state, snapshot.get("exit_code"))
        return JobInfo(
            id=job_id,
            backend=self.name,
            state=state,
            accelerator=job.accelerator,
            detail=f"on {job.session_name}",
        )

    async def logs(self, job_id: str) -> str:
        """Full log so far (reads from offset 0; use :meth:`log_tail` to follow)."""
        job = self._require(job_id)
        data, _ = await self._drain(job, offset=0)
        return data.decode(errors="replace")

    async def log_tail(self, job_id: str, *, offset: int | None = None) -> tuple[str, int]:
        """Incremental log read from ``offset`` (default: the persisted one), persisting the offset.

        This is what ``--follow`` uses: the client keeps the offset, so a disconnect or
        a fresh process resumes exactly where it left off.
        """
        job = self._require(job_id)
        start = job.log_offset if offset is None else offset
        data, new_offset = await self._drain(job, offset=start)
        job.log_offset = new_offset
        self._state.put_job(job)
        return data.decode(errors="replace"), new_offset

    async def result(self, job_id: str) -> JobResult:
        job = self._require(job_id)
        snapshot = await self._poll_until_terminal(job)
        state = job_state_from(snapshot)
        data, _ = await self._drain(job, offset=0)
        text = data.decode(errors="replace")
        exit_code = snapshot.get("exit_code")
        error = text[-400:] if state is JobState.FAILED and text else None
        return JobResult(
            id=job_id,
            backend=self.name,
            state=state,
            exit_code=int(exit_code) if isinstance(exit_code, int) else None,
            stdout=text,
            error=error,
        )

    async def cancel(self, job_id: str) -> None:
        job = self._require(job_id)
        with contextlib.suppress(JobError):
            await self._runtime.cancel(self._session(job), job_id)
        self._persist_state(job, JobState.CANCELLED, None)

    async def list_jobs(self) -> list[JobInfo]:
        """All known jobs for this backend (from the store), without probing the runtime."""
        return [
            JobInfo(id=j.id, backend=self.name, state=j.state, accelerator=j.accelerator)
            for j in self._state.list_jobs()
            if j.backend == self.name
        ]

    async def aclose(self) -> None:
        await self._transport.aclose()

    # -- internals ----------------------------------------------------------

    def _require(self, job_id: str) -> StoredJob:
        job = self._state.get_job(job_id)
        if job is None:
            raise ColabctlError(f"No such job: {job_id!r}")
        return job

    @staticmethod
    def _session(job: StoredJob) -> str:
        if job.session_name is None:
            raise ColabctlError(f"Job {job.id!r} has no session to reattach to.")
        return job.session_name

    def _persist_state(self, job: StoredJob, state: JobState, exit_code: object) -> None:
        job.state = state
        if isinstance(exit_code, int):
            job.exit_code = exit_code
        self._state.put_job(job)

    async def _poll_until_terminal(self, job: StoredJob) -> dict[str, object]:
        while True:
            snapshot = await self._poll_or_resume(job)
            if job_state_from(snapshot).is_terminal:
                self._persist_state(job, job_state_from(snapshot), snapshot.get("exit_code"))
                return snapshot
            await asyncio.sleep(self._poll_interval)

    async def _poll_or_resume(self, job: StoredJob) -> dict[str, object]:
        """Poll the job; if the runtime was reclaimed and the job is resumable, relaunch.

        ``RuntimeUnavailableError`` is the transport's definite "runtime gone" signal
        (native ``refresh_assignment`` raises it when the assignment is no longer live).
        For a ``resumable`` job we re-allocate a fresh runtime and relaunch the same
        spec — the workload is expected to resume from its own checkpoint (plan Pillar 2).
        Non-resumable jobs surface the error so the caller decides.
        """
        try:
            return await self._runtime.poll(self._session(job), job.id)
        except RuntimeUnavailableError:
            if not job.resumable:
                raise
            await self._resume(job)
            return await self._runtime.poll(self._session(job), job.id)

    async def _resume(self, job: StoredJob) -> None:
        if job.code is None:
            raise JobError(f"Job {job.id!r} has no stored code to relaunch.")
        _log.warning("job %s: runtime reclaimed, re-assigning and relaunching", job.id)
        session = await self._transport.allocate(RuntimeSpec(accelerator=job.accelerator))
        launched = await self._runtime.launch(
            session.name,
            job.id,
            script=job.code,
            requirements=job.requirements,
            timeout=job.timeout,
        )
        job.session_name = session.name
        job.pid = launched.pid
        job.remote_dir = launched.remote_dir
        job.log_offset = 0  # the new runtime starts a fresh log
        job.incarnations += 1
        job.state = JobState.RUNNING
        self._state.put_job(job)

    async def _drain(self, job: StoredJob, *, offset: int) -> tuple[bytes, int]:
        """Read the log from ``offset`` to EOF, returning the bytes and the end offset."""
        session = self._session(job)
        chunks: list[bytes] = []
        cursor = offset
        while True:
            data, new_offset = await self._runtime.tail(
                session, job.id, offset=cursor, max_bytes=_LOG_CHUNK
            )
            if data:
                chunks.append(data)
            if new_offset == cursor:  # no progress → reached EOF
                return b"".join(chunks), new_offset
            cursor = new_offset


__all__ = ["DetachedColabBackend"]
