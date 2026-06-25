"""colabctl — programmatic control of Google Colab for developers and AI agents.

Public API is intentionally small and stable; everything else is an
implementation detail behind the transport/provider abstractions.
"""

from __future__ import annotations

from colabctl.drive import DriveSync, drive_checkpoint_hooks
from colabctl.errors import (
    AcceleratorUnavailableError,
    AllocationError,
    AuthError,
    ColabctlError,
    ConfigurationError,
    ExecutionError,
    FileTransferError,
    KeepAliveError,
    KernelError,
    QuotaExceededError,
    SecretStoreError,
    TooManyAssignmentsError,
    TransportError,
)
from colabctl.lifecycle import RuntimeLifecycleManager
from colabctl.models import (
    Accelerator,
    Assignment,
    ExecutionResult,
    MachineShape,
    RuntimeProxyInfo,
    RuntimeSpec,
    SessionInfo,
    SessionStatus,
    Variant,
)
from colabctl.notebook import notebook_to_script, run_notebook, run_notebook_job
from colabctl.sdk import ColabClient, ColabSession, remote

__version__ = "0.3.7"

__all__ = [
    "Accelerator",
    "AcceleratorUnavailableError",
    "AllocationError",
    "Assignment",
    "AuthError",
    "ColabClient",
    "ColabSession",
    "ColabctlError",
    "ConfigurationError",
    "DriveSync",
    "ExecutionError",
    "ExecutionResult",
    "FileTransferError",
    "KeepAliveError",
    "KernelError",
    "MachineShape",
    "QuotaExceededError",
    "RuntimeLifecycleManager",
    "RuntimeProxyInfo",
    "RuntimeSpec",
    "SecretStoreError",
    "SessionInfo",
    "SessionStatus",
    "TooManyAssignmentsError",
    "TransportError",
    "Variant",
    "__version__",
    "drive_checkpoint_hooks",
    "notebook_to_script",
    "remote",
    "run_notebook",
    "run_notebook_job",
]
