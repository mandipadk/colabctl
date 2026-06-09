"""The colabctl SDK: ``ColabClient`` + ``ColabSession``.

The developer-facing async API over the :class:`TransportAdapter` contract. By
default it uses the sanctioned ``cli`` transport; pass ``transport="native"`` to
use the from-scratch ``/tun/m/*`` transport (opt-in). Both speak the same contract,
so this layer is transport-agnostic — and when the provider abstraction lands,
non-Colab backends slot in under the same ``Client`` without changing this API.

Example::

    async with ColabClient() as colab:
        async with await colab.allocate(gpu="T4") as session:
            result = await session.run("import torch; print(torch.cuda.get_device_name(0))")
            print(result.text)
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from colabctl.errors import ConfigurationError
from colabctl.models import (
    Accelerator,
    ExecutionResult,
    RuntimeSpec,
    SessionInfo,
)
from colabctl.transport.base import OutputCallback, TransportAdapter


def _resolve_accelerator(
    gpu: str | None, accelerator: Accelerator | None, default: Accelerator
) -> Accelerator:
    if accelerator is not None:
        return accelerator
    if gpu is not None:
        try:
            return Accelerator(gpu.upper())
        except ValueError as exc:
            raise ConfigurationError(
                f"Unknown accelerator {gpu!r}. Valid: "
                + ", ".join(a.value for a in Accelerator if a is not Accelerator.NONE)
            ) from exc
    return default


class ColabSession:
    """A handle to one live runtime. Usable as an async context manager.

    On ``async with`` exit the session is stopped unless it was opened with
    ``keep=True`` (``owns=False``).
    """

    def __init__(
        self,
        transport: TransportAdapter,
        name: str,
        *,
        info: SessionInfo | None = None,
        owns: bool = False,
    ) -> None:
        self._transport = transport
        self._name = name
        self._info = info
        self._owns = owns

    @property
    def name(self) -> str:
        return self._name

    @property
    def info(self) -> SessionInfo | None:
        return self._info

    async def run(
        self,
        code: str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        """Execute ``code`` on the runtime and return the collected result."""
        return await self._transport.execute(self._name, code, timeout=timeout, on_output=on_output)

    async def run_file(
        self,
        path: str | Path,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        """Execute a local Python file's contents on the runtime."""
        code = Path(path).read_text()
        return await self.run(code, timeout=timeout, on_output=on_output)

    async def upload(self, local_path: str | Path, remote_path: str) -> None:
        await self._transport.upload(self._name, Path(local_path), remote_path)

    async def download(self, remote_path: str, local_path: str | Path) -> None:
        await self._transport.download(self._name, remote_path, Path(local_path))

    async def status(self) -> SessionInfo | None:
        info = await self._transport.status(self._name)
        if info is not None:
            self._info = info
        return info

    async def keep_alive(self) -> None:
        """Send a keep-alive (native transport only; the CLI manages its own daemon)."""
        fn = getattr(self._transport, "keep_alive", None)
        if fn is None:
            raise NotImplementedError(
                f"The {self._transport.name!r} transport manages keep-alive itself."
            )
        await fn(self._name)

    async def stop(self) -> None:
        await self._transport.stop(self._name)

    async def __aenter__(self) -> ColabSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns:
            await self.stop()


class ColabClient:
    """Entry point to drive Colab. Build once; allocate/attach sessions from it."""

    def __init__(
        self,
        transport: TransportAdapter | None = None,
        *,
        transport_name: str = "cli",
        auth_mode: str = "adc",
        colab_bin: str = "colab",
    ) -> None:
        self._transport = transport or self._build_transport(
            transport_name, auth_mode=auth_mode, colab_bin=colab_bin
        )

    @staticmethod
    def _build_transport(name: str, *, auth_mode: str, colab_bin: str) -> TransportAdapter:
        if name == "cli":
            from colabctl.transport.cli import ColabCliTransport

            return ColabCliTransport(auth=auth_mode, colab_bin=colab_bin)
        if name == "native":
            from colabctl.auth import ADCAuthProvider
            from colabctl.transport.native import NativeColabTransport

            return NativeColabTransport.create(ADCAuthProvider())
        raise ConfigurationError(f"Unknown transport {name!r}. Use 'cli' or 'native'.")

    @property
    def transport(self) -> TransportAdapter:
        return self._transport

    async def allocate(
        self,
        *,
        gpu: str | None = None,
        accelerator: Accelerator | None = None,
        name: str | None = None,
        keep: bool = False,
    ) -> ColabSession:
        """Allocate a runtime and return a :class:`ColabSession`.

        ``keep=True`` leaves the runtime running when the session context exits.
        """
        acc = _resolve_accelerator(gpu, accelerator, default=Accelerator.T4)
        spec = RuntimeSpec(accelerator=acc, name=name)
        info = await self._transport.allocate(spec)
        return ColabSession(self._transport, info.name, info=info, owns=not keep)

    def attach(self, name: str) -> ColabSession:
        """Attach to an existing session by name (never auto-stops it)."""
        return ColabSession(self._transport, name, owns=False)

    async def list_sessions(self) -> list[SessionInfo]:
        return await self._transport.list_sessions()

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> ColabClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
