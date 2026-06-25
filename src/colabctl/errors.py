"""Exception taxonomy for colabctl.

Every error a caller can encounter is a subclass of :class:`ColabctlError`, so
``except ColabctlError`` is always sufficient as a catch-all. The hierarchy mirrors
the layers of the system (auth → transport → allocation/execution/file/keepalive)
so callers and the provider-abstraction's fallback logic can branch precisely —
e.g. ``QuotaExceededError`` should trigger failover to another backend, while a
``ParseError`` indicates the pinned CLI contract drifted and must fail loudly.

Every error also carries a **stable machine-readable code**, a **category**, and an optional
**remediation** hint, surfaced via :meth:`ColabctlError.to_dict` across the MCP/SDK boundary so
an agent can react programmatically (retry, fail over, re-auth, raise the budget) rather than
parse a human string (Phase 3.9.2).
"""

from __future__ import annotations

from typing import Any


class ColabctlError(Exception):
    """Base class for every error raised by colabctl.

    Subclasses set :attr:`code` (a stable SCREAMING_SNAKE identifier), :attr:`category`
    (the system layer), and optionally :attr:`remediation` (a one-line fix hint).
    """

    code: str = "COLABCTL_ERROR"
    category: str = "general"
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """A machine-readable view: ``{error, code, category, message[, remediation, ...]}``.

        Subclasses with extra context (argv, exit code, accelerator, traceback) extend this.
        """
        data: dict[str, Any] = {
            "error": type(self).__name__,
            "code": self.code,
            "category": self.category,
            "message": str(self),
        }
        if self.remediation:
            data["remediation"] = self.remediation
        return data


# --- configuration / auth ---------------------------------------------------


class ConfigurationError(ColabctlError):
    """Invalid or missing configuration (profiles, env, settings)."""

    code = "CONFIGURATION"
    category = "config"


class SecretStoreError(ColabctlError):
    """The secret store could not read/write a credential."""

    code = "SECRET_STORE"
    category = "config"


class StateError(ColabctlError):
    """The persistent state store could not be read, parsed, or written.

    Raised for unrecoverable conditions (e.g. a state document written by a newer
    colabctl whose schema this version cannot safely downgrade). Recoverable
    corruption is quarantined and a fresh document returned instead — see
    :mod:`colabctl.state.store`.
    """

    code = "STATE"
    category = "state"


class AuthError(ColabctlError):
    """Authentication or credential acquisition/refresh failed."""

    code = "AUTH"
    category = "auth"
    remediation = "Re-authenticate with `colabctl auth login`."


class ScopeError(AuthError):
    """The credential is missing a required OAuth scope (e.g. ``colaboratory``)."""

    code = "SCOPE"
    remediation = "Re-auth including the Colab scope: `colabctl auth login`."


# --- transport --------------------------------------------------------------


class TransportError(ColabctlError):
    """A transport (CLI subprocess, native /tun/m/*, browser bridge) failed."""

    code = "TRANSPORT"
    category = "transport"


class CLIError(TransportError):
    """The ``google-colab-cli`` subprocess failed.

    Carries the captured streams so callers can diagnose contract drift.
    """

    code = "CLI"

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

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.returncode is not None:
            data["returncode"] = self.returncode
        return data


class ParseError(CLIError):
    """The CLI produced output we could not parse against the pinned grammar.

    This is intentionally distinct from :class:`CLIError`: it means the
    *contract changed*, and the adapter should fail loudly rather than guess.
    """

    code = "PARSE"
    remediation = (
        "google-colab-cli output drifted from the pinned grammar; pin its version or update."
    )


# --- allocation -------------------------------------------------------------


class AllocationError(TransportError):
    """A runtime/VM could not be allocated."""

    code = "ALLOCATION"
    category = "allocation"


class TooManyAssignmentsError(AllocationError):
    """Backend returned HTTP 412 — the account already has too many assignments."""

    code = "TOO_MANY_ASSIGNMENTS"
    remediation = "Reclaim orphaned runtimes: `colabctl -t native gc --release-orphans`."


class QuotaExceededError(AllocationError):
    """The account's compute-unit / usage quota is exhausted (failover candidate)."""

    code = "QUOTA_EXCEEDED"
    remediation = "Wait for quota reset, or route to another backend with `--allow`."


class AcceleratorUnavailableError(AllocationError):
    """The requested accelerator is unavailable or unentitled (e.g. no A100 on this tier)."""

    code = "ACCELERATOR_UNAVAILABLE"
    remediation = "Pick a different `--gpu` or backend (`colabctl cost --gpu X` shows coverage)."

    def __init__(self, message: str, *, accelerator: str | None = None) -> None:
        super().__init__(message)
        self.accelerator = accelerator

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.accelerator is not None:
            data["accelerator"] = self.accelerator
        return data


# --- runtime / execution ----------------------------------------------------


class RuntimeUnavailableError(TransportError):
    """The runtime was reclaimed, expired, or is otherwise unreachable."""

    code = "RUNTIME_UNAVAILABLE"
    category = "runtime"
    remediation = "Resubmit; a `--resumable` detached job auto-resumes from its checkpoint."


class KeepAliveError(TransportError):
    """Keep-alive failed (e.g. the ADC serviceusage 403 documented in Phase 0)."""

    code = "KEEPALIVE"
    category = "runtime"


class KernelError(ColabctlError):
    """The Jupyter kernel failed to start, restart, or respond."""

    code = "KERNEL"
    category = "execution"


class ExecutionError(ColabctlError):
    """Code execution raised inside the kernel.

    Mirrors a Jupyter ``error`` reply so the original traceback is preserved.
    """

    code = "EXECUTION"
    category = "execution"

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

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        if self.ename is not None:
            data["ename"] = self.ename
        if self.evalue is not None:
            data["evalue"] = self.evalue
        return data


class ExecutionTimeoutError(ExecutionError):
    """Execution exceeded the caller's timeout budget."""

    code = "EXECUTION_TIMEOUT"
    remediation = "Increase `--timeout`, or submit it as a durable `--detach --resumable` job."


# --- file transfer ----------------------------------------------------------


class FileTransferError(ColabctlError):
    """Uploading to / downloading from a runtime (or Drive) failed."""

    code = "FILE_TRANSFER"
    category = "file"


# --- detached jobs ------------------------------------------------------------


class JobError(ColabctlError):
    """A detached-job operation (launch/poll/tail/cancel) failed on the runtime."""

    code = "JOB"
    category = "job"


# --- SDK --------------------------------------------------------------------


class SerializationError(ColabctlError):
    """Marshalling a function/args/result for remote execution failed."""

    code = "SERIALIZATION"
    category = "sdk"
