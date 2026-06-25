"""``KernelJobRuntime`` — drive detached jobs over any ``TransportAdapter``.

A thin async layer that turns the pure builders in :mod:`colabctl.jobs.codes` into
operations on a live session: ``launch`` writes the payload and spawns the supervisor
detached; ``poll`` reads the on-VM status; ``tail`` streams the log by byte offset; and
``cancel`` signals the job's process group. Because every call is a *short* kernel exec
(the workload runs in its own process, not as a foreground cell), the kernel stays free
for keep-alive/checkpoint, and a dropped connection costs only a reconnect.

Transport-agnostic on purpose: it speaks only ``execute()``, so it works over the
native transport (the intended host) and any other that runs Python and returns stdout.
"""

from __future__ import annotations

from dataclasses import dataclass

from colabctl.backends.base import JobState
from colabctl.errors import JobError
from colabctl.jobs.codes import (
    DEFAULT_JOBS_ROOT,
    build_cancel_code,
    build_launch_code,
    build_poll_code,
    build_tail_code,
    parse_launch_pid,
    parse_status_frame,
    parse_tail_frame,
    remote_dir_for,
)
from colabctl.transport.base import TransportAdapter

#: Map the runner's ``status.json`` state strings to the domain :class:`JobState`.
_STATE_MAP = {
    "running": JobState.RUNNING,
    "succeeded": JobState.SUCCEEDED,
    "failed": JobState.FAILED,
    "cancelled": JobState.CANCELLED,
    "missing": JobState.PENDING,  # launched but status.json not written yet
    "unknown": JobState.UNKNOWN,
}


def job_state_from(status: dict[str, object]) -> JobState:
    """Translate a poll snapshot's ``state`` into a :class:`JobState`.

    A snapshot that still says ``running`` but whose runner process is no longer alive
    (``runner_alive`` is False) means the runner was killed without writing a terminal state
    (OOM, SIGKILL) — resolve it to FAILED so the job can't lie RUNNING forever.
    """
    if status.get("state") == "running" and status.get("runner_alive") is False:
        return JobState.FAILED
    return _STATE_MAP.get(str(status.get("state", "unknown")), JobState.UNKNOWN)


@dataclass
class LaunchResult:
    pid: int
    remote_dir: str


class KernelJobRuntime:
    """Launch/poll/tail/cancel detached jobs on a session via its transport."""

    def __init__(self, transport: TransportAdapter, *, root: str = DEFAULT_JOBS_ROOT) -> None:
        self._transport = transport
        self._root = root

    def remote_dir(self, job_id: str) -> str:
        return remote_dir_for(job_id, root=self._root)

    async def launch(
        self,
        session: str,
        job_id: str,
        *,
        script: str,
        requirements: list[str] | None = None,
        timeout: float | None = None,
        created_at: float | None = None,
        env: dict[str, str] | None = None,
    ) -> LaunchResult:
        code = build_launch_code(
            job_id,
            script=script,
            requirements=requirements,
            timeout=timeout,
            root=self._root,
            created_at=created_at,
            env=env,
        )
        result = await self._transport.execute(session, code)
        if not result.ok:
            raise JobError(f"failed to launch job {job_id!r}: {result.error or result.text[:300]}")
        return LaunchResult(pid=parse_launch_pid(result.text), remote_dir=self.remote_dir(job_id))

    async def poll(self, session: str, job_id: str) -> dict[str, object]:
        result = await self._transport.execute(session, build_poll_code(job_id, root=self._root))
        if not result.ok:
            raise JobError(f"failed to poll job {job_id!r}: {result.error or result.text[:300]}")
        return parse_status_frame(result.text)

    async def state(self, session: str, job_id: str) -> JobState:
        return job_state_from(await self.poll(session, job_id))

    async def tail(
        self, session: str, job_id: str, *, offset: int = 0, max_bytes: int = 65536
    ) -> tuple[bytes, int]:
        """Return ``(new_bytes, new_offset)`` of ``log.txt`` from ``offset``."""
        result = await self._transport.execute(
            session, build_tail_code(job_id, offset=offset, max_bytes=max_bytes, root=self._root)
        )
        if not result.ok:
            raise JobError(f"failed to tail job {job_id!r}: {result.error or result.text[:300]}")
        return parse_tail_frame(result.text)

    async def cancel(self, session: str, job_id: str) -> bool:
        """Signal the job's process group; return whether a live process was signalled."""
        result = await self._transport.execute(session, build_cancel_code(job_id, root=self._root))
        if not result.ok:
            raise JobError(f"failed to cancel job {job_id!r}: {result.error or result.text[:300]}")
        return bool(parse_status_frame(result.text).get("cancelled", False))


__all__ = ["KernelJobRuntime", "LaunchResult", "job_state_from"]
