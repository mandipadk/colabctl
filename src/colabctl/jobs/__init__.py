"""Detached jobs — submit work that survives the client, the connection, and reclaim.

The substrate (Pillar 2): a job's code is written to the runtime and launched as a
**detached supervised process** under ``<remote_dir>/`` (see :mod:`colabctl.jobs.codes`),
so the kernel stays a control plane — short execs poll, tail, and cancel — and the job's
truth lives on the VM's disk, reachable from any process. :class:`KernelJobRuntime`
drives this over any :class:`~colabctl.transport.base.TransportAdapter`.
"""

from __future__ import annotations

from colabctl.jobs.backend import DetachedColabBackend
from colabctl.jobs.codes import (
    DEFAULT_JOBS_ROOT,
    RUNNER_SOURCE,
    build_cancel_code,
    build_launch_code,
    build_poll_code,
    build_tail_code,
    parse_launch_pid,
    parse_status_frame,
    parse_tail_frame,
    remote_dir_for,
)
from colabctl.jobs.runtime import KernelJobRuntime, LaunchResult, job_state_from

__all__ = [
    "DEFAULT_JOBS_ROOT",
    "RUNNER_SOURCE",
    "DetachedColabBackend",
    "KernelJobRuntime",
    "LaunchResult",
    "build_cancel_code",
    "build_launch_code",
    "build_poll_code",
    "build_tail_code",
    "job_state_from",
    "parse_launch_pid",
    "parse_status_frame",
    "parse_tail_frame",
    "remote_dir_for",
]
