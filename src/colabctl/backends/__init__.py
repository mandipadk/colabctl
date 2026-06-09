"""Provider abstraction: pluggable batch backends + capability-based routing.

- :class:`Backend` — the submit/status/logs/result/cancel contract.
- :class:`ColabBackend` — Colab via an interactive transport (sanctioned default).
- :class:`ModalBackend` — gVisor GPU sandboxes (best for agent code).
- :class:`VertexBackend` — sanctioned, headless, deadline-bound GPU jobs.
- :class:`BackendRouter` — selects a backend by capability and fails over on infra errors.

HF Jobs / Kaggle / IaaS are registered-but-deferred (Phase 4).
"""

from __future__ import annotations

from colabctl.backends.base import (
    Backend,
    BackendCapabilities,
    JobInfo,
    JobResult,
    JobSpec,
    JobState,
)
from colabctl.backends.colab import ColabBackend
from colabctl.backends.hf_backend import HFJobsBackend
from colabctl.backends.modal_backend import ModalBackend
from colabctl.backends.router import BackendRouter
from colabctl.backends.vertex_backend import VertexBackend

__all__ = [
    "Backend",
    "BackendCapabilities",
    "BackendRouter",
    "ColabBackend",
    "HFJobsBackend",
    "JobInfo",
    "JobResult",
    "JobSpec",
    "JobState",
    "ModalBackend",
    "VertexBackend",
]
