"""``NativeColabTransport`` — the from-scratch, opt-in Colab transport.

Composes the verified ``/tun/m/*`` :class:`ColabBackendClient` with a Jupyter kernel to
implement the full :class:`TransportAdapter` contract: allocate a runtime, run code
(typed Jupyter outputs), transfer files, and tear down. Disabled by default per the
sanctioned-default ToS posture; this is the co-primary that ensures no CLI lock-in.

**Durable across processes (Pillar 1).** Every allocation is persisted to the
:class:`~colabctl.state.StateStore` (metadata) plus the secret store (the proxy token,
a credential), so a runtime created in one process can be *attached* from another —
``colabctl new`` then later ``exec -s NAME`` now works, ``stop`` never silently
no-ops, and ``gc`` reclaims orphaned assignments. Cold attach reconnects via the
GET-only :meth:`ColabBackendClient.refresh_assignment` (live-verified Phase A §②:
same runtime, fresh token), falling back to a cached token when one is still valid.

The backend client, kernel factory, state store, and secret store are all injected, so
the whole lifecycle is unit-testable with fakes (no network, no real keychain/home).
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel

from colabctl.auth.base import AuthProvider
from colabctl.errors import (
    ConfigurationError,
    FileTransferError,
    KernelError,
    RuntimeUnavailableError,
    TransportError,
)
from colabctl.models import (
    Assignment,
    ExecutionResult,
    RuntimeProxyInfo,
    RuntimeSpec,
    SessionInfo,
    SessionStatus,
)
from colabctl.observability import get_logger
from colabctl.secrets.base import SecretStore
from colabctl.state import RecordState, StateStore, StoredSession, utcnow
from colabctl.transport.base import Capabilities, OutputCallback, TransportAdapter
from colabctl.transport.native.client import ColabBackendClient
from colabctl.transport.native.contents import ContentsTransfer
from colabctl.transport.native.kernel import (
    KernelProtocol,
    default_kernel_factory,
)

KernelFactory = Callable[[str, str], KernelProtocol]

_log = get_logger("transport.native")

_NATIVE_ENV = "COLABCTL_ENABLE_NATIVE"
#: Reuse a cached proxy token only if it has comfortably more than this left, so a
#: token that would expire mid-use triggers a refresh instead.
_TOKEN_REUSE_MARGIN_S = 120.0
#: Budget for the keep-alive activity ping. Kernel executes queue behind a running
#: cell, so an unbounded ping would wedge the keep-alive loop for the cell's whole
#: duration (plan §5.5) — bound it and let the caller treat timeout as "kernel busy".
_KEEPALIVE_PING_TIMEOUT_S = 30.0


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


def _try_default_secrets() -> SecretStore | None:
    """Best-effort secret store for caching proxy tokens (``None`` if unavailable).

    The proxy token is a credential, so when a backing store exists we cache it there
    (never in ``state.json``). If neither an OS keychain nor an encrypted-file passphrase
    is available, we return ``None`` and the attach path simply *refreshes* the token
    from the stored notebook id — so the ``secrets`` extra is an optimization, not a
    requirement, for cross-process attach.
    """
    if os.environ.get("COLABCTL_SECRET_PASSPHRASE"):
        with contextlib.suppress(Exception):
            from colabctl.secrets import EncryptedFileSecretStore

            return EncryptedFileSecretStore()
        return None
    with contextlib.suppress(Exception):
        import keyring  # noqa: F401  (probe: only use the keychain if it imports)

        from colabctl.secrets import KeyringSecretStore

        return KeyringSecretStore()
    return None


@dataclass
class _Session:
    info: SessionInfo
    assignment: Assignment
    notebook_id: uuid.UUID | None = None
    kernel: KernelProtocol | None = None
    proxy_deadline: float | None = None  # time.monotonic() when the proxy token expires


class ReconcileReport(BaseModel):
    """The diff between the server's live assignments and our local records."""

    orphan_endpoints: list[str] = []  # live on the server, no active local record
    stale_sessions: list[str] = []  # active records whose runtime is gone
    live_tracked: list[str] = []  # endpoints both live on the server and tracked locally


class GcReport(BaseModel):
    """What :meth:`NativeColabTransport.gc` found and did."""

    reconcile: ReconcileReport
    released_orphans: list[str] = []
    pruned_records: list[str] = []


class NativeColabTransport(TransportAdapter):
    """Drive Colab through colabctl's own backend client + kernel, with durable state."""

    name = "native"

    def __init__(
        self,
        *,
        client: ColabBackendClient,
        kernel_factory: KernelFactory = default_kernel_factory,
        state: StateStore | None = None,
        secrets: SecretStore | None = None,
        account: str = "adc-default",
        authuser: int = 0,
        _owned_http: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client
        self._kernel_factory = kernel_factory
        self._owned_http = _owned_http
        self._contents = ContentsTransfer(client)
        self._sessions: dict[str, _Session] = {}
        self._state = state if state is not None else StateStore()
        self._secrets = secrets if secrets is not None else _try_default_secrets()
        self._account = account
        self._authuser = authuser

    @classmethod
    def create(
        cls,
        auth: AuthProvider,
        *,
        kernel_factory: KernelFactory = default_kernel_factory,
        timeout: float = 60.0,
        allow_native: bool = False,
        state: StateStore | None = None,
        secrets: SecretStore | None = None,
        account: str = "adc-default",
    ) -> NativeColabTransport:
        """Build a transport with a real HTTP client bound to ``auth``.

        Opt-in gated: requires ``allow_native=True`` or ``COLABCTL_ENABLE_NATIVE=1``
        (the reverse-engineered path is disabled by default — see ToS posture).
        """
        if not allow_native:
            require_native_opt_in()
        http = httpx.AsyncClient(timeout=timeout)
        client = ColabBackendClient(http, token_provider=auth.as_token_callable())
        return cls(
            client=client,
            kernel_factory=kernel_factory,
            state=state,
            secrets=secrets,
            account=account,
            _owned_http=http,
        )

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
                "File transfer uses the Jupyter contents/files REST API over the runtime "
                "proxy (chunked upload, ranged download); no kernel message-size ceiling.",
            ],
        )

    async def allocate(self, spec: RuntimeSpec) -> SessionInfo:
        # Generate the notebook id ourselves (instead of letting the client mint and
        # discard one) so we can persist it — it is the seed for reattach + refresh.
        notebook_id = uuid.uuid4()
        assignment = await self._client.assign(
            accelerator=spec.accelerator, notebook_id=notebook_id
        )
        name = spec.name or assignment.endpoint
        info = SessionInfo(
            name=name,
            endpoint=assignment.endpoint,
            accelerator=assignment.accelerator,
            variant=assignment.variant,
            status=SessionStatus.IDLE,
        )
        self._sessions[name] = _Session(
            info=info,
            assignment=assignment,
            notebook_id=notebook_id,
            proxy_deadline=self._deadline(assignment),
        )
        self._persist(name, notebook_id, assignment)
        return info

    async def attach(self, name: str) -> SessionInfo:
        """Reconnect to a session created (possibly by another process) and recorded.

        Loads the persisted record, reconnects (cached token if still valid, else a
        GET-only refresh that mints a fresh one and verifies the runtime still exists),
        and returns its :class:`SessionInfo`. Raises :class:`RuntimeUnavailableError`
        if there is no active record or the runtime was reclaimed.
        """
        return (await self._ensure(name)).info

    async def refresh_token(self, name: str) -> bool:
        """Refresh the runtime-proxy token in place for a session (no disruptive re-assign).

        Uses the GET-only refresh primitive (Phase A §②: same runtime, fresh token), so a
        long session's credential is renewed without tearing down the runtime or kernel.
        The live kernel keeps its existing connection; the fresh token is what subsequent
        reconnects and REST transfers use. Returns ``False`` if there is no notebook id to
        refresh from. This is the non-disruptive answer to §5.10.
        """
        sess = await self._ensure(name)
        if sess.notebook_id is None:
            return False
        fresh = await self._client.refresh_assignment(
            sess.notebook_id, accelerator=sess.assignment.accelerator
        )
        sess.assignment = fresh
        sess.proxy_deadline = self._deadline(fresh)
        self._persist(name, sess.notebook_id, fresh)
        return True

    def seconds_until_proxy_expiry(self, name: str) -> float | None:
        """Seconds until this session's runtime-proxy token expires (None if unknown).

        Consults the in-memory deadline first, then the persisted wall-clock expiry, so
        a lifecycle manager can refresh/re-assign before the credential dies even across
        processes. Live-verified non-disruptive refresh (Phase A §②) is the durable
        answer; see :meth:`ColabBackendClient.refresh_assignment`.
        """
        sess = self._sessions.get(name)
        if sess is not None and sess.proxy_deadline is not None:
            return sess.proxy_deadline - time.monotonic()
        record = self._state.get_session(name)
        if record is not None:
            return record.proxy_token_seconds_remaining()
        return None

    async def list_sessions(self) -> list[SessionInfo]:
        """List runtimes the server reports live, recovering their friendly names.

        Server truth (``/tun/m/assignments``) is the source of liveness; names are
        recovered from in-memory and persisted records (falling back to the endpoint),
        so a runtime shows under the name you gave it even from a fresh process.
        """
        assignments = await self._client.list_assignments()
        mem_name = {s.assignment.endpoint: s.info.name for s in self._sessions.values()}
        rec_name = {
            r.endpoint: r.name
            for r in self._state.list_sessions()
            if r.transport == self.name and r.state is RecordState.ACTIVE
        }
        return [
            SessionInfo(
                name=mem_name.get(a.endpoint) or rec_name.get(a.endpoint) or a.endpoint,
                endpoint=a.endpoint,
                accelerator=a.accelerator,
                variant=a.variant,
                status=SessionStatus.UNKNOWN,
            )
            for a in assignments
        ]

    async def status(self, name: str) -> SessionInfo | None:
        sess = self._sessions.get(name)
        if sess is not None:
            return sess.info
        record = self._state.get_session(name)
        if (
            record is None
            or record.transport != self.name
            or record.state is not RecordState.ACTIVE
        ):
            return None
        return SessionInfo(
            name=record.name,
            endpoint=record.endpoint,
            accelerator=record.accelerator,
            variant=record.variant,
            status=SessionStatus.UNKNOWN,
        )

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
        proxy_url, proxy_token = await self._proxy(name)
        await self._contents.upload(proxy_url, proxy_token, local_path, remote_path)

    async def download(self, name: str, remote_path: str, local_path: Path) -> None:
        proxy_url, proxy_token = await self._proxy(name)
        await self._contents.download(proxy_url, proxy_token, remote_path, local_path)

    async def stop(self, name: str) -> None:
        """Release the runtime and forget it — never a silent no-op (the v0.2 leak bug).

        Resolves the endpoint from in-memory state, the persisted record, or (if the
        caller passed a raw endpoint) the live server list. If nothing matches, raises
        :class:`RuntimeUnavailableError` instead of pretending to have stopped something.
        The local record/token are removed only once release is confirmed.
        """
        sess = self._sessions.pop(name, None)
        endpoint: str | None = None
        if sess is not None:
            if sess.kernel is not None:
                with contextlib.suppress(Exception):
                    await sess.kernel.stop()
            endpoint = sess.assignment.endpoint
        record = self._state.get_session(name)
        if endpoint is None and record is not None:
            endpoint = record.endpoint
        if endpoint is None:
            # Unknown locally — accept a raw endpoint only if it is actually live.
            if not any(a.endpoint == name for a in await self._client.list_assignments()):
                raise RuntimeUnavailableError(f"No such native session to stop: {name!r}")
            endpoint = name
        await self._release(endpoint)
        self._delete_token(name)
        self._state.delete_session(name)

    async def keep_alive(self, name: str) -> None:
        """Best-effort keep-alive: register kernel activity.

        The official RuntimeService keep-alive RPC is unusable under token auth
        (401 api-key / 403 bearer — live-confirmed, PHASE0-FINDINGS §2; it needs
        browser session cookies), so we instead execute a trivial statement to mark
        the kernel active. This is best-effort, not a guaranteed lease extension —
        long jobs should keep the kernel genuinely busy and checkpoint/re-assign on
        reclamation.

        The ping is time-bounded: kernel executes queue behind a running cell, and a
        busy kernel is already registering activity, so blocking the keep-alive loop
        on it would be pure harm (plan §5.5).
        """
        kernel = await self._kernel(name)
        await kernel.execute("None", timeout=_KEEPALIVE_PING_TIMEOUT_S)

    async def is_live(self, name: str) -> bool | None:
        """Whether this session's runtime is still assigned server-side.

        ``True``/``False`` from the live ``/tun/m/assignments`` list; ``None`` when the
        endpoint cannot be resolved (no in-memory session and no stored record). The
        lifecycle manager uses this to distinguish *reclaimed* from a transient
        transport blip before destroying a warm runtime (plan §5.4).
        """
        sess = self._sessions.get(name)
        endpoint = sess.assignment.endpoint if sess is not None else None
        if endpoint is None:
            record = self._state.get_session(name)
            if record is not None and record.transport == self.name:
                endpoint = record.endpoint
        if endpoint is None:
            return None
        return any(a.endpoint == endpoint for a in await self._client.list_assignments())

    async def interrupt(self, name: str) -> None:
        """Interrupt the running cell on this session's kernel (REST; Phase A §④).

        Lets an agent stop a runaway computation without killing the whole runtime
        (which the v0.2 transport could only do). Requires a started kernel.
        """
        sess = await self._ensure(name)
        rpi = sess.assignment.runtime_proxy_info
        if rpi is None:
            raise RuntimeUnavailableError(f"Session {name!r} has no runtime proxy info.")
        kernel = await self._kernel(name)
        kernel_id = kernel.kernel_id
        if kernel_id is None:
            raise KernelError(f"Session {name!r} kernel id unknown; cannot interrupt.")
        await self._client.interrupt_kernel(rpi.url, kernel_id, proxy_token=rpi.token)

    async def reconnect(self, name: str) -> None:
        """Re-dial this session's kernel after a dropped websocket (keeps in-kernel state).

        No-op if no kernel is connected yet. The server-side kernel survives a websocket
        drop (Phase A §③), so this restores the connection without losing state; only
        idempotent work should be re-issued afterward.
        """
        sess = await self._ensure(name)
        if sess.kernel is not None:
            await sess.kernel.reconnect()

    async def ccu_info(self) -> object | None:
        """Best-effort compute-unit balance/usage from the backend (raw, undocumented shape).

        Surfaced by ``colabctl quota`` so a Colab Pro user can see their compute-unit
        standing before allocating an expensive accelerator.
        """
        return await self._client.ccu_info()

    async def reconcile(self) -> ReconcileReport:
        """Diff live server assignments against local records (no mutation)."""
        live = {a.endpoint for a in await self._client.list_assignments()}
        active = {
            r.endpoint: r.name
            for r in self._state.list_sessions()
            if r.transport == self.name and r.state is RecordState.ACTIVE
        }
        return ReconcileReport(
            orphan_endpoints=sorted(live - set(active)),
            stale_sessions=sorted(name for ep, name in active.items() if ep not in live),
            live_tracked=sorted(live & set(active)),
        )

    async def gc(self, *, release_orphans: bool = False, prune_stale: bool = True) -> GcReport:
        """Reconcile and optionally clean up: release orphan runtimes, prune dead records.

        ``release_orphans`` unassigns server-side runtimes with no local record (paid
        leaks — e.g. from a v0.2 client that lost track on exit). ``prune_stale`` drops
        local records whose runtime is gone. Default is the safe, non-destructive path
        (prune dead records only); the CLI surfaces ``--release-orphans`` explicitly.
        """
        report = await self.reconcile()
        released: list[str] = []
        if release_orphans:
            for endpoint in report.orphan_endpoints:
                try:
                    await self._release(endpoint)
                    released.append(endpoint)
                except TransportError as exc:
                    _log.warning("native gc: could not release orphan %s: %s", endpoint, exc)
        pruned: list[str] = []
        if prune_stale:
            for stale in report.stale_sessions:
                self._sessions.pop(stale, None)
                self._delete_token(stale)
                self._state.delete_session(stale)
                pruned.append(stale)
        return GcReport(reconcile=report, released_orphans=released, pruned_records=pruned)

    async def aclose(self) -> None:
        for sess in list(self._sessions.values()):
            if sess.kernel is not None:
                with contextlib.suppress(Exception):
                    await sess.kernel.stop()
        self._sessions.clear()
        if self._owned_http is not None:
            await self._owned_http.aclose()

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _deadline(assignment: Assignment) -> float | None:
        rpi = assignment.runtime_proxy_info
        if rpi is not None and rpi.token_expires_in_seconds:
            return time.monotonic() + rpi.token_expires_in_seconds
        return None

    async def _ensure(self, name: str) -> _Session:
        """Return the live in-memory session, attaching it from the store if needed."""
        sess = self._sessions.get(name)
        if sess is not None:
            return sess
        record = self._state.get_session(name)
        if record is None or record.transport != self.name:
            raise RuntimeUnavailableError(f"No such native session: {name!r}")
        if record.state is not RecordState.ACTIVE:
            raise RuntimeUnavailableError(f"Native session {name!r} was terminated.")
        sess = await self._attach_from_record(record)
        self._sessions[name] = sess
        return sess

    async def _attach_from_record(self, record: StoredSession) -> _Session:
        notebook_id = uuid.UUID(record.notebook_id)
        cached = self._load_token(record.name)
        if (
            cached
            and record.proxy_url
            and not record.proxy_token_expired(margin=_TOKEN_REUSE_MARGIN_S)
        ):
            # Fast path: reuse the cached, comfortably-unexpired token. Liveness is
            # verified lazily on first execute — a dead runtime surfaces as
            # RuntimeUnavailableError, which the lifecycle manager re-assigns around.
            remaining = record.proxy_token_seconds_remaining() or 0.0
            assignment = Assignment(
                endpoint=record.endpoint,
                accelerator=record.accelerator,
                variant=record.variant,
                machine_shape=record.machine_shape,
                runtime_proxy_info=RuntimeProxyInfo(
                    token=cached, token_expires_in_seconds=int(remaining), url=record.proxy_url
                ),
            )
        else:
            # Refresh: GET-only reattach mints a fresh token and confirms the runtime is
            # still live (raises RuntimeUnavailableError if it was reclaimed).
            assignment = await self._client.refresh_assignment(
                notebook_id, accelerator=record.accelerator
            )
            self._persist(record.name, notebook_id, assignment)
        info = SessionInfo(
            name=record.name,
            endpoint=assignment.endpoint,
            accelerator=assignment.accelerator,
            variant=assignment.variant,
            status=SessionStatus.UNKNOWN,
        )
        return _Session(
            info=info,
            assignment=assignment,
            notebook_id=notebook_id,
            proxy_deadline=self._deadline(assignment),
        )

    async def _proxy(self, name: str) -> tuple[str, str]:
        """The (proxy_url, proxy_token) for a session's runtime, for REST transfer."""
        sess = await self._ensure(name)
        rpi = sess.assignment.runtime_proxy_info
        if rpi is None:
            raise FileTransferError(
                f"Session {name!r} has no runtime proxy info; cannot transfer files."
            )
        return rpi.url, rpi.token

    async def _kernel(self, name: str) -> KernelProtocol:
        sess = await self._ensure(name)
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

    async def _release(self, endpoint: str) -> None:
        """Unassign a runtime, confirming it is gone before declaring success.

        Avoids both a false error when the runtime was already reclaimed and a silent
        leak when it is still live: on unassign failure we re-check the server list and
        only swallow the error if the endpoint is genuinely gone.
        """
        try:
            await self._client.unassign(endpoint)
        except TransportError:
            if any(a.endpoint == endpoint for a in await self._client.list_assignments()):
                raise
            _log.info(
                "native: unassign(%s) failed but runtime is gone; treated as released", endpoint
            )

    # -- persistence helpers ------------------------------------------------

    def _persist(self, name: str, notebook_id: uuid.UUID, assignment: Assignment) -> None:
        rpi = assignment.runtime_proxy_info
        token_ref = self._save_token(name, rpi.token) if rpi is not None else None
        expires_at = None
        if rpi is not None and rpi.token_expires_in_seconds:
            expires_at = utcnow() + timedelta(seconds=rpi.token_expires_in_seconds)
        self._state.put_session(
            StoredSession(
                name=name,
                transport=self.name,
                notebook_id=str(notebook_id),
                endpoint=assignment.endpoint,
                proxy_url=rpi.url if rpi is not None else None,
                proxy_token_ref=token_ref,
                proxy_token_expires_at=expires_at,
                accelerator=assignment.accelerator,
                variant=assignment.variant,
                machine_shape=assignment.machine_shape,
                account=self._account,
                authuser=self._authuser,
            )
        )

    def _token_key(self, name: str) -> str:
        return f"native-proxy:{self._account}:{name}"

    def _save_token(self, name: str, token: str) -> str | None:
        if self._secrets is None:
            return None
        key = self._token_key(name)
        try:
            self._secrets.set(key, token)
            return key
        except Exception as exc:  # optional cache — never fail allocation on it
            _log.warning("native: could not cache proxy token in secret store: %s", exc)
            return None

    def _load_token(self, name: str) -> str | None:
        if self._secrets is None:
            return None
        try:
            return self._secrets.get(self._token_key(name))
        except Exception as exc:
            _log.warning("native: could not read cached proxy token: %s", exc)
            return None

    def _delete_token(self, name: str) -> None:
        if self._secrets is None:
            return
        with contextlib.suppress(Exception):
            self._secrets.delete(self._token_key(name))
