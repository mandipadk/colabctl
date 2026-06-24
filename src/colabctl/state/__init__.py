"""Durable, cross-process state for colabctl runtimes and jobs.

A single JSON document under ``~/.colabctl/state.json`` (override with
``$COLABCTL_HOME``) records the runtime assignments and detached jobs that must
outlive the process that created them. This is the dependency root of the
durability work: native *attach*, truthful ``stop``/``status``/``gc``, and the
detached-job manager all read and write through :class:`StateStore`.

Credentials are never stored here — only metadata; the runtime-proxy token lives in
:mod:`colabctl.secrets`, referenced by key. See :mod:`colabctl.state.models`.
"""

from __future__ import annotations

from colabctl.state.models import (
    SCHEMA_VERSION,
    JobEvent,
    RecordState,
    StateDocument,
    StoredJob,
    StoredSession,
    utcnow,
)
from colabctl.state.store import StateStore, default_home

__all__ = [
    "SCHEMA_VERSION",
    "JobEvent",
    "RecordState",
    "StateDocument",
    "StateStore",
    "StoredJob",
    "StoredSession",
    "default_home",
    "utcnow",
]
