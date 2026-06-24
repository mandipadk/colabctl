"""Persistent state records — the durable, cross-process truth about runtimes.

The v0.2 native transport tracked sessions only in process memory: a ``new`` in one
process was invisible to the next (``exec -s NAME`` failed), and ``stop`` silently
no-opped while the assignment kept burning compute units. These records are the
durable index that the native *attach* path, the detached-job manager, and
``colabctl gc`` read and write, so a session survives the process that created it.

**Credentials never live here.** The runtime-proxy token is itself a credential; it
is stored in the pluggable secret store (:mod:`colabctl.secrets`) and referenced from
a record by key (``proxy_token_ref``). This document carries only metadata — safe to
write as plaintext JSON under ``~/.colabctl/state.json``.

The one field that makes reattach and non-disruptive token refresh possible is
``notebook_id``: the UUID seed for the ``nbh`` query value. Re-running the assign
GET pre-flight with the *same* ``nbh`` returns the existing assignment (with fresh
``runtimeProxyInfo``) instead of allocating a new runtime — so we must persist it.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from colabctl.backends.base import JobState
from colabctl.models import Accelerator, MachineShape, Variant

#: Bump when the on-disk shape changes incompatibly; :class:`StateDocument` records
#: the version it was written with so an older client refuses to misread a newer file.
SCHEMA_VERSION = 1


def utcnow() -> datetime:
    """Timezone-aware current UTC time (the persisted wall clock)."""
    return datetime.now(UTC)


class RecordState(enum.StrEnum):
    """Lifecycle of a stored session *record* (distinct from the runtime's status).

    ``ACTIVE`` while we believe the assignment is live; ``TERMINATED`` once it has
    been released or reconciled away (absent from the server's assignment list).
    Terminated records are kept until ``gc`` prunes them, so nothing is lost
    silently — the opposite of the v0.2 in-memory ``pop``.
    """

    ACTIVE = "ACTIVE"
    TERMINATED = "TERMINATED"


class StoredSession(BaseModel):
    """One runtime assignment, persisted so any process can find/attach/release it."""

    name: str
    transport: str = "native"

    #: UUID string seed for the ``nbh`` value — REQUIRED for reattach + token refresh.
    notebook_id: str
    endpoint: str

    #: Jupyter server URL behind the runtime proxy (from ``runtimeProxyInfo.url``).
    proxy_url: str | None = None
    #: Secret-store key under which the ``X-Colab-Runtime-Proxy-Token`` is held.
    proxy_token_ref: str | None = None
    #: Wall-clock expiry of the proxy token (derived from ``tokenExpiresInSeconds``).
    proxy_token_expires_at: datetime | None = None

    accelerator: Accelerator = Accelerator.NONE
    variant: Variant = Variant.DEFAULT
    machine_shape: MachineShape = MachineShape.STANDARD

    #: Account the runtime belongs to (email, or ``"adc-default"``) + its authuser.
    account: str = "adc-default"
    authuser: int = 0

    state: RecordState = RecordState.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)

    def proxy_token_seconds_remaining(self, *, now: datetime | None = None) -> float | None:
        """Seconds until the proxy token expires, or ``None`` if expiry is unknown."""
        if self.proxy_token_expires_at is None:
            return None
        return (self.proxy_token_expires_at - (now or utcnow())).total_seconds()

    def proxy_token_expired(self, *, now: datetime | None = None, margin: float = 0.0) -> bool:
        """True if the proxy token is expired (or within ``margin`` seconds of it).

        Unknown expiry is treated as expired — callers should refresh rather than
        trust a token we can't reason about.
        """
        remaining = self.proxy_token_seconds_remaining(now=now)
        return remaining is None or remaining <= margin


class JobEvent(BaseModel):
    """One entry in a job's append-only state-transition timeline.

    Turns the opaque scalar ``(state, incarnations=7)`` into an auditable history: *when*
    it changed, *from/to* which state, on which *incarnation*, and *why*. The bounded-resume
    path and (later) the spend audit ledger both append here.
    """

    at: datetime = Field(default_factory=utcnow)
    from_state: JobState
    to_state: JobState
    incarnation: int = 1
    reason: str | None = None


class StoredJob(BaseModel):
    """A detached job, persisted so its lifecycle outlives the launching process.

    The runtime-side source of truth lives under ``remote_dir`` on the VM
    (``status.json``/``exit_code``/``log.txt``); this record is the client-side
    index that lets ``job logs --follow``/``job result`` resume from any process.
    Fleshed out fully in Pillar 2; defined here so the store can hold it from day one.
    """

    id: str
    session_name: str | None = None
    backend: str = "colab"
    state: JobState = JobState.PENDING

    accelerator: Accelerator = Accelerator.NONE
    requirements: list[str] = Field(default_factory=list)
    #: Inline code to (re)launch on the runtime; needed to relaunch after reclamation.
    code: str | None = None
    script_path: str | None = None
    timeout: int | None = None
    #: Whether the workload resumes idempotently from its checkpoint after a re-assign.
    resumable: bool = False

    #: ``/content/.colabctl/jobs/<id>`` on the VM (None until launched).
    remote_dir: str | None = None
    pid: int | None = None
    #: Byte offset into ``log.txt`` already consumed — lets ``--follow`` resume exactly.
    log_offset: int = 0
    exit_code: int | None = None
    #: How many runtimes this job has run on (incremented on each auto-resume).
    incarnations: int = 1
    #: Hard ceiling on incarnations — auto-resume refuses to re-allocate past this, so a
    #: flapping runtime can't loop allocating paid GPUs forever (the worst footgun).
    max_incarnations: int = 3
    #: Append-only transition history (when/why each state change + on which incarnation).
    events: list[JobEvent] = Field(default_factory=list)
    #: Stitched logs from prior incarnations + boundary markers, so a re-assign doesn't
    #: silently reset the log view to zero. ``logs``/``result`` prepend this to the current
    #: incarnation's live log; the current runtime's log is always read live from the VM.
    archived_log: str = ""

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class StateDocument(BaseModel):
    """The root of ``state.json``: a schema version plus the session/job indexes."""

    schema_version: int = SCHEMA_VERSION
    sessions: dict[str, StoredSession] = Field(default_factory=dict)
    jobs: dict[str, StoredJob] = Field(default_factory=dict)


__all__ = [
    "SCHEMA_VERSION",
    "RecordState",
    "StateDocument",
    "StoredJob",
    "StoredSession",
    "utcnow",
]
