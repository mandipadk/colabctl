"""``NativeColabTransport`` — the from-scratch, opt-in Colab transport.

Composes the verified ``/tun/m/*`` :class:`ColabBackendClient` with a Jupyter
kernel to implement the full :class:`TransportAdapter` contract: allocate a runtime,
run code (typed Jupyter outputs), transfer files (kernel base64), and tear down.
Disabled by default per the sanctioned-default ToS posture; this is the co-primary
that ensures no CLI lock-in, and the home of the API-key-only keep-alive fix.

The backend client and kernel factory are injected, so the whole lifecycle is
unit-testable with fakes (no network).
"""

from __future__ import annotations

import base64
import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from colabctl.auth.base import AuthProvider
from colabctl.errors import ConfigurationError, FileTransferError, RuntimeUnavailableError
from colabctl.models import (
    Assignment,
    ExecutionResult,
    RuntimeSpec,
    SessionInfo,
    SessionStatus,
)
from colabctl.transport.base import Capabilities, OutputCallback, TransportAdapter
from colabctl.transport.native.client import ColabBackendClient
from colabctl.transport.native.kernel import (
    KernelProtocol,
    build_download_code,
    build_upload_code,
    default_kernel_factory,
    parse_b64_payload,
)

KernelFactory = Callable[[str, str], KernelProtocol]

_UPLOAD_SENTINEL = "COLABCTL_UPLOAD_OK"
_NATIVE_ENV = "COLABCTL_ENABLE_NATIVE"


def native_opt_in_enabled() -> bool:
    """True if the reverse-engineered native transport has been explicitly enabled."""
    return os.environ.get(_NATIVE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def require_native_opt_in() -> None:
    """Raise unless the native transport has been opted into (env or allow_native)."""
    if not native_opt_in_enabled():
        raise ConfigurationError(
            "The native /tun/m/* transport is reverse-engineered and DISABLED BY DEFAULT "
            "per the sanctioned ToS posture. To opt in, set "
            f"{_NATIVE_ENV}=1 (or pass allow_native=True) and accept the higher fragility "
            "and abuse-detection exposure — see DIRECTIVES.md and spikes/PHASE0-FINDINGS.md §2."
        )


@dataclass
class _Session:
    info: SessionInfo
    assignment: Assignment
    kernel: KernelProtocol | None = None
    proxy_deadline: float | None = None  # time.monotonic() when the proxy token expires


class NativeColabTransport(TransportAdapter):
    """Drive Colab through colabctl's own backend client + kernel."""

    name = "native"

    def __init__(
        self,
        *,
        client: ColabBackendClient,
        kernel_factory: KernelFactory = default_kernel_factory,
        _owned_http: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client
        self._kernel_factory = kernel_factory
        self._owned_http = _owned_http
        self._sessions: dict[str, _Session] = {}

    @classmethod
    def create(
        cls,
        auth: AuthProvider,
        *,
        kernel_factory: KernelFactory = default_kernel_factory,
        timeout: float = 60.0,
        allow_native: bool = False,
    ) -> NativeColabTransport:
        """Build a transport with a real HTTP client bound to ``auth``.

        Opt-in gated: requires ``allow_native=True`` or ``COLABCTL_ENABLE_NATIVE=1``
        (the reverse-engineered path is disabled by default — see ToS posture).
        """
        if not allow_native:
            require_native_opt_in()
        http = httpx.AsyncClient(timeout=timeout)
        client = ColabBackendClient(http, token_provider=auth.as_token_callable())
        return cls(client=client, kernel_factory=kernel_factory, _owned_http=http)

    # -- contract -----------------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            interactive=True,
            streaming_output=True,  # incremental via kernel execute_interactive output_hook
            headless=True,
            selectable_accelerator=True,
            keepalive=False,
            file_transfer=True,
            notebook_execution=False,
            caveats=[
                "Reverse-engineered /tun/m/* transport — opt-in, disabled by default "
                "per the sanctioned ToS posture.",
                "The RuntimeService keep-alive RPC is UNUSABLE under token auth "
                "(401 api-key / 403 bearer, live-confirmed — PHASE0-FINDINGS §2): it needs "
                "browser session cookies. keep_alive() therefore does a best-effort kernel "
                "activity ping; for long jobs, rely on the workload keeping the kernel busy "
                "and checkpoint/re-assign on idle reclamation.",
                "File transfer runs over the kernel (base64); large files are not yet "
                "streamed/chunked.",
            ],
        )

    async def allocate(self, spec: RuntimeSpec) -> SessionInfo:
        assignment = await self._client.assign(accelerator=spec.accelerator)
        name = spec.name or assignment.endpoint
        info = SessionInfo(
            name=name,
            endpoint=assignment.endpoint,
            accelerator=assignment.accelerator,
            variant=assignment.variant,
            status=SessionStatus.IDLE,
        )
        deadline: float | None = None
        rpi = assignment.runtime_proxy_info
        if rpi is not None and rpi.token_expires_in_seconds:
            deadline = time.monotonic() + rpi.token_expires_in_seconds
        self._sessions[name] = _Session(info=info, assignment=assignment, proxy_deadline=deadline)
        return info

    def seconds_until_proxy_expiry(self, name: str) -> float | None:
        """Seconds until this session's runtime-proxy token expires (None if unknown).

        Lets a lifecycle manager proactively re-assign before the credential dies. The
        RuntimeService keep-alive RPC can't refresh tokens under token auth (Phase 0 §2),
        so re-assign (with checkpoint/restore) is the durable answer for long sessions.
        """
        sess = self._sessions.get(name)
        if sess is None or sess.proxy_deadline is None:
            return None
        return sess.proxy_deadline - time.monotonic()

    async def list_sessions(self) -> list[SessionInfo]:
        assignments = await self._client.list_assignments()
        name_by_endpoint = {s.assignment.endpoint: s.info.name for s in self._sessions.values()}
        return [
            SessionInfo(
                name=name_by_endpoint.get(a.endpoint, a.endpoint),
                endpoint=a.endpoint,
                accelerator=a.accelerator,
                variant=a.variant,
                status=SessionStatus.UNKNOWN,
            )
            for a in assignments
        ]

    async def status(self, name: str) -> SessionInfo | None:
        sess = self._sessions.get(name)
        return sess.info if sess is not None else None

    async def execute(
        self,
        name: str,
        code: str,
        *,
        timeout: float | None = None,
        on_output: OutputCallback | None = None,
    ) -> ExecutionResult:
        kernel = await self._kernel(name)
        return await kernel.execute(code, timeout=timeout, on_output=on_output)

    async def upload(self, name: str, local_path: Path, remote_path: str) -> None:
        b64 = base64.b64encode(local_path.read_bytes()).decode()
        result = await self.execute(name, build_upload_code(remote_path, b64))
        if not result.ok or _UPLOAD_SENTINEL not in result.text:
            detail = result.error or result.text[:200]
            raise FileTransferError(f"Upload of {local_path} → {remote_path} failed: {detail}")

    async def download(self, name: str, remote_path: str, local_path: Path) -> None:
        result = await self.execute(name, build_download_code(remote_path))
        if not result.ok:
            raise FileTransferError(
                f"Download of {remote_path} failed: {result.error or result.text[:200]}"
            )
        local_path.write_bytes(parse_b64_payload(result.text))

    async def stop(self, name: str) -> None:
        sess = self._sessions.pop(name, None)
        if sess is None:
            return
        if sess.kernel is not None:
            await sess.kernel.stop()
        await self._client.unassign(sess.assignment.endpoint)

    async def keep_alive(self, name: str) -> None:
        """Best-effort keep-alive: register kernel activity.

        The official RuntimeService keep-alive RPC is unusable under token auth
        (401 api-key / 403 bearer — live-confirmed, PHASE0-FINDINGS §2; it needs
        browser session cookies), so we instead execute a trivial statement to mark
        the kernel active. This is best-effort, not a guaranteed lease extension —
        long jobs should keep the kernel genuinely busy and checkpoint/re-assign on
        reclamation.
        """
        self._require(name)
        kernel = await self._kernel(name)
        await kernel.execute("None")

    async def aclose(self) -> None:
        for sess in list(self._sessions.values()):
            if sess.kernel is not None:
                with contextlib.suppress(Exception):
                    await sess.kernel.stop()
        self._sessions.clear()
        if self._owned_http is not None:
            await self._owned_http.aclose()

    # -- internals ----------------------------------------------------------

    def _require(self, name: str) -> _Session:
        sess = self._sessions.get(name)
        if sess is None:
            raise RuntimeUnavailableError(f"No such native session: {name!r}")
        return sess

    async def _kernel(self, name: str) -> KernelProtocol:
        sess = self._require(name)
        if sess.kernel is None:
            rpi = sess.assignment.runtime_proxy_info
            if rpi is None:
                raise RuntimeUnavailableError(
                    f"Session {name!r} has no runtime proxy info; cannot connect kernel."
                )
            kernel = self._kernel_factory(rpi.url, rpi.token)
            await kernel.start()
            sess.kernel = kernel
        return sess.kernel
