"""Exception taxonomy for colabctl.

Every error a caller can encounter is a subclass of :class:`ColabctlError`, so
``except ColabctlError`` is always sufficient as a catch-all. The hierarchy mirrors
the layers of the system (auth → transport → allocation/execution/file/keepalive)
so callers and the provider-abstraction's fallback logic can branch precisely —
e.g. ``QuotaExceededError`` should trigger failover to another backend, while a
``ParseError`` indicates the pinned CLI contract drifted and must fail loudly.
"""

from __future__ import annotations


class ColabctlError(Exception):
    """Base class for every error raised by colabctl."""


# --- configuration / auth ---------------------------------------------------


class ConfigurationError(ColabctlError):
    """Invalid or missing configuration (profiles, env, settings)."""


class SecretStoreError(ColabctlError):
    """The secret store could not read/write a credential."""


class StateError(ColabctlError):
    """The persistent state store could not be read, parsed, or written.

    Raised for unrecoverable conditions (e.g. a state document written by a newer
    colabctl whose schema this version cannot safely downgrade). Recoverable
    corruption is quarantined and a fresh document returned instead — see
    :mod:`colabctl.state.store`.
    """


class AuthError(ColabctlError):
    """Authentication or credential acquisition/refresh failed."""


class ScopeError(AuthError):
    """The credential is missing a required OAuth scope (e.g. ``colaboratory``)."""


# --- transport --------------------------------------------------------------


class TransportError(ColabctlError):
    """A transport (CLI subprocess, native /tun/m/*, browser bridge) failed."""


class CLIError(TransportError):
    """The ``google-colab-cli`` subprocess failed.

    Carries the captured streams so callers can diagnose contract drift.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: list[str] | None = None,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ParseError(CLIError):
    """The CLI produced output we could not parse against the pinned grammar.

    This is intentionally distinct from :class:`CLIError`: it means the
    *contract changed*, and the adapter should fail loudly rather than guess.
    """


# --- allocation -------------------------------------------------------------


class AllocationError(TransportError):
    """A runtime/VM could not be allocated."""


class TooManyAssignmentsError(AllocationError):
    """Backend returned HTTP 412 — the account already has too many assignments."""


class QuotaExceededError(AllocationError):
    """The account's compute-unit / usage quota is exhausted (failover candidate)."""


class AcceleratorUnavailableError(AllocationError):
    """The requested accelerator is unavailable or unentitled (e.g. no A100 on this tier)."""

    def __init__(self, message: str, *, accelerator: str | None = None) -> None:
        super().__init__(message)
        self.accelerator = accelerator


# --- runtime / execution ----------------------------------------------------


class RuntimeUnavailableError(TransportError):
    """The runtime was reclaimed, expired, or is otherwise unreachable."""


class KeepAliveError(TransportError):
    """Keep-alive failed (e.g. the ADC serviceusage 403 documented in Phase 0)."""


class KernelError(ColabctlError):
    """The Jupyter kernel failed to start, restart, or respond."""


class ExecutionError(ColabctlError):
    """Code execution raised inside the kernel.

    Mirrors a Jupyter ``error`` reply so the original traceback is preserved.
    """

    def __init__(
        self,
        message: str,
        *,
        ename: str | None = None,
        evalue: str | None = None,
        traceback: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.ename = ename
        self.evalue = evalue
        self.traceback = traceback or []


class ExecutionTimeoutError(ExecutionError):
    """Execution exceeded the caller's timeout budget."""


# --- file transfer ----------------------------------------------------------


class FileTransferError(ColabctlError):
    """Uploading to / downloading from a runtime (or Drive) failed."""


# --- detached jobs ------------------------------------------------------------


class JobError(ColabctlError):
    """A detached-job operation (launch/poll/tail/cancel) failed on the runtime."""


# --- SDK --------------------------------------------------------------------


class SerializationError(ColabctlError):
    """Marshalling a function/args/result for remote execution failed."""
