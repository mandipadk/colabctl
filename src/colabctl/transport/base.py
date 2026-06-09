"""The transport contract.

Every way of driving Colab — the official CLI, our native ``/tun/m/*`` client,
the browser bridge — implements :class:`TransportAdapter`. This is the single
seam that enforces the project's governing directive: *no CLI lock-in*. The SDK,
provider abstraction, and MCP server speak only to this interface, so a regressed
or missing CLI degrades to the native transport (or another backend) without any
change above this line.

The interface is async because runtime allocation, execution streaming, and file
transfer are all I/O-bound and long-running; synchronous convenience wrappers live
in the SDK layer, not here.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from pydantic import BaseModel

from colabctl.models import ExecutionResult, Output, RuntimeSpec, SessionInfo


class Capabilities(BaseModel):
    """What a transport can and cannot do.

    The provider abstraction uses capability feature-detection to route work and
    set expectations (e.g. pick a streaming-capable transport for an interactive
    session, or avoid one that can't keep a runtime alive for a long batch job).
    """

    name: str
    interactive: bool = True  # supports incremental execute on a live kernel
    streaming_output: bool = False  # can stream outputs as they arrive
    headless: bool = True  # works with zero human/browser involvement
    selectable_accelerator: bool = True
    keepalive: bool = False  # can hold a runtime past idle reclamation
    file_transfer: bool = True
    notebook_execution: bool = False  # can run a whole .ipynb with output capture
    # Honest, machine-readable notes about limitations (e.g. the ADC keep-alive 403).
    caveats: list[str] = []


# A callback invoked for each output as it streams in (when supported).
OutputCallback = Callable[[Output], None]


class TransportAdapter(abc.ABC):
    """Abstract base every transport implements."""

    #: Stable, lowercase transport id (e.g. ``"cli"``, ``"native"``).
    name: str = "transport"

    @property
    @abc.abstractmethod
    def capabilities(self) -> Capabilities:
        """Static description of what this transport supports."""

    @abc.abstractmethod
    async def allocate(self, spec: RuntimeSpec) -> SessionInfo:
        """Allocate (or attach to) a runtime matching ``spec`` and return its info.

        Raises an :class:`~colabctl.errors.AllocationError` subclass on quota,
        entitlement, or concurrency failures so callers/fallback can branch.
        """

    @abc.abstractmethod
    async def list_sessions(self) -> list[SessionInfo]:
        """List sessions known to this transport (local + server-visible)."""

    @abc.abstractmethod
    async def status(self, name: str) -> SessionInfo | None:
        """Return one session's status, or ``None`` if unknown."""

    @abc.abstractmethod
    async def execute(
        self,
        name: str,
        code: str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        """Run ``code`` on session ``name`` and return the collected result.

        If ``on_output`` is given and the transport supports streaming, it is
        called for each output as it arrives (outputs are still aggregated into
        the returned :class:`ExecutionResult`).
        """

    @abc.abstractmethod
    async def upload(self, name: str, local_path: Path, remote_path: str) -> None:
        """Upload a local file to the runtime."""

    @abc.abstractmethod
    async def download(self, name: str, remote_path: str, local_path: Path) -> None:
        """Download a file from the runtime."""

    @abc.abstractmethod
    async def stop(self, name: str) -> None:
        """Terminate the session and release the runtime."""

    async def stream(
        self, name: str, code: str, *, timeout: float | None = None
    ) -> AsyncIterator[Output]:
        """Optional: execute and yield outputs as an async iterator.

        Default implementation runs non-streaming and yields the collected
        outputs at the end; streaming transports should override.
        """
        result = await self.execute(name, code, timeout=timeout)
        for output in result.outputs:
            yield output

    async def aclose(self) -> None:
        """Release any transport-level resources (connections, subprocesses).

        Default is a no-op; transports that hold connections or subprocesses override.
        """
        return None
