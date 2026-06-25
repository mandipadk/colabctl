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
from dataclasses import dataclass, field
from datetime import timedelta

from colabctl.allocation import DEFAULT_MAX_ATTEMPTS, AllocationGate
from colabctl.backends.base import (
    Backend,
    BackendCapabilities,
    JobInfo,
    JobResult,
    JobSpec,
    JobState,
)
from colabctl.errors import AllocationError, ColabctlError, JobError, RuntimeUnavailableError
from colabctl.jobs.codes import DEFAULT_JOBS_ROOT
from colabctl.jobs.runtime import KernelJobRuntime, job_state_from
from colabctl.models import RuntimeSpec
from colabctl.observability import correlation_context, get_logger
from colabctl.state import AuditEvent, JobEvent, StateStore, StoredJob, utcnow
from colabctl.transport.base import TransportAdapter

_log = get_logger("jobs.backend")


@dataclass
class JobGcReport:
    """What a ``gc_jobs`` pass did: which records it reconciled to LOST and which it pruned."""

    reconciled: list[str] = field(default_factory=list)  # marked LOST (their runtime is gone)
    pruned: list[str] = field(default_factory=list)  # terminal records deleted past the TTL


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
        max_incarnations: int = DEFAULT_MAX_ATTEMPTS,
        gate: AllocationGate | None = None,
    ) -> None:
        self._transport = transport
        self._state = state if state is not None else StateStore()
        self._runtime = KernelJobRuntime(transport, root=root)
        self._poll_interval = poll_interval
        self._max_incarnations = max_incarnations
        self._gate = gate if gate is not None else AllocationGate()

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

    def _apply_tracking(
        self,
        track: str | None,
        job_id: str,
        *,
        base_env: dict[str, str],
        base_reqs: list[str],
        code: str,
    ) -> tuple[dict[str, str], str, list[str]]:
        """Resolve tracking env (creds from the secret store — re-resolved per launch, never
        persisted), wrap the script with the autolog preamble/postamble, and add the lib."""
        env = dict(base_env or {})
        reqs = list(base_reqs or [])
        if track:
            from colabctl.secrets import default_secret_store
            from colabctl.tracking import (
                requirements_for,
                resolve_tracking_env,
                tracking_postamble,
                tracking_preamble,
            )

            env.update(resolve_tracking_env(track, job_id, secret_get=default_secret_store().get))
            reqs += requirements_for(track)
            code = f"{tracking_preamble(track)}\n{code}\n{tracking_postamble(track)}"
        return env, code, reqs

    async def submit(self, spec: JobSpec) -> JobInfo:
        job_id = f"colab-{uuid.uuid4().hex[:10]}"
        session = await self._transport.allocate(
            RuntimeSpec(accelerator=spec.accelerator, name=spec.name)
        )
        env, script, reqs = self._apply_tracking(
            spec.track,
            job_id,
            base_env=spec.env,
            base_reqs=list(spec.requirements),
            code=spec.resolved_code(),
        )
        launched = await self._runtime.launch(
            session.name,
            job_id,
            script=script,
            requirements=reqs,
            timeout=spec.timeout,
            env=env,
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
                env=dict(spec.env or {}),
                track=spec.track,
                max_incarnations=self._max_incarnations,
                remote_dir=launched.remote_dir,
                pid=launched.pid,
                events=[
                    JobEvent(
                        from_state=JobState.PENDING,
                        to_state=JobState.RUNNING,
                        incarnation=1,
                        reason="submitted",
                    )
                ],
            )
        )
        self._state.record_audit(
            AuditEvent(
                action="submit",
                backend=self.name,
                accelerator=spec.accelerator,
                job_id=job_id,
                session_id=session.name,
                incarnation=1,
                detail="detached job submitted" + (" (resumable)" if spec.resumable else ""),
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
        self._persist_state(
            job, state, snapshot.get("exit_code"), reason=self._terminal_reason(snapshot, state)
        )
        return JobInfo(
            id=job_id,
            backend=self.name,
            state=state,
            accelerator=job.accelerator,
            detail=f"on {job.session_name}",
        )

    async def logs(self, job_id: str) -> str:
        """Full log so far — prior incarnations (stitched, with boundary markers) followed by
        the current runtime's live log. Use :meth:`log_tail` to follow the current runtime."""
        job = self._require(job_id)
        data, _ = await self._drain(job, offset=0)
        return job.archived_log + data.decode(errors="replace")

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

    def _capture_lineage(self, job: StoredJob, text: str) -> None:
        """Record a tracking run's id/URL (printed by the job) into the audit ledger, once."""
        try:
            import json as _json

            from colabctl.tracking import parse_lineage

            lineage = parse_lineage(text)
            already = any(e.action == "lineage" for e in self._state.list_audit(job_id=job.id))
            if lineage and not already:
                self._state.record_audit(
                    AuditEvent(
                        action="lineage",
                        backend=self.name,
                        job_id=job.id,
                        detail=_json.dumps({"track": job.track, **lineage}),
                    )
                )
        except Exception:
            pass  # lineage bookkeeping must never break result()

    async def result(self, job_id: str) -> JobResult:
        job = self._require(job_id)
        snapshot = await self._poll_until_terminal(job)
        state = job_state_from(snapshot)
        data, _ = await self._drain(job, offset=0)
        text = job.archived_log + data.decode(errors="replace")
        if job.track:
            self._capture_lineage(job, text)
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

    async def gc_jobs(self, *, ttl_hours: float = 168.0, reconcile: bool = True) -> JobGcReport:
        """Reconcile job records against live sessions and prune stale terminal records.

        Without this, records accumulate forever and a job whose runtime was reclaimed lies
        ``RUNNING`` indefinitely. ``reconcile`` marks a **non-resumable** job whose session is
        gone as ``FAILED`` (an honest terminal state, recorded as an event) — resumable jobs
        are left alone since they recover on the next poll. Terminal records whose last
        transition is older than ``ttl_hours`` are then deleted.
        """
        report = JobGcReport()
        live_names: set[str] = set()
        if reconcile:
            with contextlib.suppress(ColabctlError):
                live_names = {s.name for s in await self._transport.list_sessions()}
        cutoff = utcnow() - timedelta(hours=ttl_hours)
        for job in [j for j in self._state.list_jobs() if j.backend == self.name]:
            if (
                reconcile
                and not job.state.is_terminal
                and not job.resumable
                and (job.session_name is None or job.session_name not in live_names)
            ):
                job.events.append(
                    JobEvent(
                        from_state=job.state,
                        to_state=JobState.FAILED,
                        incarnation=job.incarnations,
                        reason="runtime gone (reconciled by gc)",
                    )
                )
                job.state = JobState.FAILED
                self._state.put_job(job)
                report.reconciled.append(job.id)
            elif job.state.is_terminal:
                last = job.events[-1].at if job.events else job.created_at
                if last < cutoff:
                    self._state.delete_job(job.id)
                    report.pruned.append(job.id)
        return report

    async def remove_job(self, job_id: str) -> bool:
        """Delete a single job record (does not touch the runtime)."""
        return self._state.delete_job(job_id)

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

    @staticmethod
    def _terminal_reason(snapshot: dict[str, object], state: JobState) -> str | None:
        if state is JobState.FAILED and snapshot.get("runner_alive") is False:
            return "runner process died"
        return None

    def _persist_state(
        self, job: StoredJob, state: JobState, exit_code: object, *, reason: str | None = None
    ) -> None:
        if state != job.state:
            job.events.append(
                JobEvent(
                    from_state=job.state,
                    to_state=state,
                    incarnation=job.incarnations,
                    reason=reason,
                )
            )
        job.state = state
        if isinstance(exit_code, int):
            job.exit_code = exit_code
        self._state.put_job(job)

    async def _poll_until_terminal(self, job: StoredJob) -> dict[str, object]:
        while True:
            snapshot = await self._poll_or_resume(job)
            state = job_state_from(snapshot)
            if state.is_terminal:
                self._persist_state(
                    job,
                    state,
                    snapshot.get("exit_code"),
                    reason=self._terminal_reason(snapshot, state),
                )
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
        # Bound the re-allocation: refuse (and mark the job failed) once the incarnation
        # cap is hit, and back off between attempts — so a flapping runtime can't loop
        # allocating paid GPUs forever. ``_resume`` only runs on RuntimeUnavailableError
        # (a "runtime gone" signal), never on a user-code failure, so this never retries a
        # deterministically-broken job.
        try:
            await self._gate.before_attempt(
                job.incarnations + 1, job.max_incarnations, what=f"job {job.id!r}"
            )
        except AllocationError:
            job.events.append(
                JobEvent(
                    from_state=job.state,
                    to_state=JobState.FAILED,
                    incarnation=job.incarnations,
                    reason="exceeded max incarnations",
                )
            )
            job.state = JobState.FAILED
            self._state.put_job(job)
            raise
        # Bind the job's correlation ids so every log line during the reclaim→re-allocate
        # (transport, retry/backoff) is attributable to this job + incarnation, not just the
        # one line that names it — the 12h cross-reassignment debugging case.
        with correlation_context(job_id=job.id, incarnation=str(job.incarnations + 1)):
            _log.warning(
                "job %s: runtime reclaimed, re-assigning (incarnation %d/%d)",
                job.id,
                job.incarnations + 1,
                job.max_incarnations,
            )
            session = await self._transport.allocate(RuntimeSpec(accelerator=job.accelerator))
            # Re-apply tracking on resume (creds re-resolved from the secret store, not persisted).
            env, script, reqs = self._apply_tracking(
                job.track, job.id, base_env=job.env, base_reqs=job.requirements, code=job.code
            )
            launched = await self._runtime.launch(
                session.name,
                job.id,
                script=script,
                requirements=reqs,
                timeout=job.timeout,
                env=env,
            )
        prior_state = job.state
        # Record the incarnation boundary in the stitched log so the re-assign is visible
        # and the prior runtime's logs aren't silently replaced by a fresh-from-zero view.
        job.archived_log += (
            f"\n--- [colabctl] incarnation {job.incarnations} runtime reclaimed; "
            f"resuming as incarnation {job.incarnations + 1} ---\n"
        )
        job.session_name = session.name
        job.pid = launched.pid
        job.remote_dir = launched.remote_dir
        job.log_offset = 0  # the new runtime starts a fresh log
        job.incarnations += 1
        job.events.append(
            JobEvent(
                from_state=prior_state,
                to_state=JobState.RUNNING,
                incarnation=job.incarnations,
                reason="runtime reclaimed; re-assigned",
            )
        )
        job.state = JobState.RUNNING
        self._state.put_job(job)
        self._state.record_audit(
            AuditEvent(
                action="resume",
                backend=self.name,
                accelerator=job.accelerator,
                job_id=job.id,
                session_id=session.name,
                incarnation=job.incarnations,
                detail="runtime reclaimed; auto-resumed from checkpoint",
            )
        )

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
