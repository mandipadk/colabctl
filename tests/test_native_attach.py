"""Cross-process durability for NativeColabTransport: attach, truthful stop, gc.

Two transports sharing one ``StateStore`` (and one fake backend client) stand in for
two ``colabctl`` processes. These guard the Pillar-1 fixes: a session created in one
process is attachable from another, ``stop`` never silently no-ops (the v0.2 leak), and
``gc`` reconciles/reclaims. No network, no real keychain/home.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from colabctl.errors import RuntimeUnavailableError, TransportError
from colabctl.models import (
    Accelerator,
    Assignment,
    ExecutionResult,
    RuntimeProxyInfo,
    RuntimeSpec,
    StreamOutput,
    Variant,
)
from colabctl.secrets import MemorySecretStore
from colabctl.state import StateStore
from colabctl.transport.native.adapter import NativeColabTransport


class FakeClient:
    """In-memory stand-in for the /tun/m/* backend (shared server state)."""

    def __init__(self) -> None:
        self.assignments: dict[str, Assignment] = {}  # endpoint -> Assignment
        self._nb_to_ep: dict[str, str] = {}  # notebook_id -> endpoint
        self.unassigned: list[str] = []
        self.interrupts: list[tuple[str, str, str]] = []
        self.assign_calls = 0
        self.refresh_calls = 0
        self._tok = 0

    def _make(self, endpoint: str, accelerator: Accelerator) -> Assignment:
        self._tok += 1
        return Assignment(
            endpoint=endpoint,
            accelerator=accelerator,
            variant=Variant.GPU,
            runtime_proxy_info=RuntimeProxyInfo(
                token=f"ptok{self._tok}",
                token_expires_in_seconds=600,
                url=f"https://x/tun/m/{endpoint}",
            ),
        )

    def add_orphan(self, endpoint: str) -> None:
        self.assignments[endpoint] = self._make(endpoint, Accelerator.T4)

    def reclaim(self, endpoint: str) -> None:
        self.assignments.pop(endpoint, None)

    async def assign(self, *, accelerator: Accelerator = Accelerator.T4, notebook_id=None):
        self.assign_calls += 1
        endpoint = f"gpu-{str(notebook_id)[:8]}"
        assignment = self._make(endpoint, accelerator)
        self.assignments[endpoint] = assignment
        self._nb_to_ep[str(notebook_id)] = endpoint
        return assignment

    async def refresh_assignment(self, notebook_id, *, accelerator: Accelerator = Accelerator.T4):
        self.refresh_calls += 1
        endpoint = self._nb_to_ep.get(str(notebook_id))
        if endpoint is None or endpoint not in self.assignments:
            raise RuntimeUnavailableError("reclaimed")
        return self._make(endpoint, accelerator)  # same runtime, fresh token

    async def list_assignments(self) -> list[Assignment]:
        return list(self.assignments.values())

    async def unassign(self, endpoint: str) -> None:
        if endpoint not in self.assignments:
            raise TransportError(f"no such assignment {endpoint}")
        self.unassigned.append(endpoint)
        del self.assignments[endpoint]

    async def interrupt_kernel(self, proxy_url: str, kernel_id: str, *, proxy_token: str) -> None:
        self.interrupts.append((proxy_url, kernel_id, proxy_token))


class FakeKernel:
    def __init__(self) -> None:
        self.started = False
        self.codes: list[str] = []
        self.timeouts: list[float | None] = []
        self.reconnected = False

    @property
    def kernel_id(self) -> str | None:
        return "kid-1"

    async def start(self) -> None:
        self.started = True

    async def execute(self, code, *, timeout=None, on_output=None) -> ExecutionResult:
        self.codes.append(code)
        self.timeouts.append(timeout)
        return ExecutionResult(status="ok", outputs=[StreamOutput(name="stdout", text="ok")])

    async def restart(self) -> None: ...

    async def reconnect(self) -> None:
        self.reconnected = True

    async def stop(self) -> None: ...


def _mk(
    client: FakeClient, state: StateStore, secrets=None
) -> tuple[NativeColabTransport, list[FakeKernel]]:
    kernels: list[FakeKernel] = []

    def factory(url: str, token: str) -> FakeKernel:
        k = FakeKernel()
        kernels.append(k)
        return k

    transport = NativeColabTransport(
        client=client,  # type: ignore[arg-type]
        kernel_factory=factory,  # type: ignore[arg-type]
        state=state,
        secrets=secrets,
    )
    return transport, kernels


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    return StateStore(home=tmp_path / "home")


# -- attach ------------------------------------------------------------------


async def test_attach_in_second_process_via_refresh(state: StateStore) -> None:
    client = FakeClient()
    t1, _ = _mk(client, state, secrets=None)
    await t1.allocate(RuntimeSpec(name="job1"))
    endpoint = state.get_session("job1").endpoint  # type: ignore[union-attr]

    # Second "process": no in-memory session, no cached token → must refresh.
    t2, kernels = _mk(client, state, secrets=None)
    info = await t2.attach("job1")
    assert info.endpoint == endpoint
    assert client.refresh_calls == 1  # GET-only reattach
    result = await t2.execute("job1", "print(1)")
    assert result.ok and kernels and kernels[0].started


async def test_attach_uses_cached_token_without_refresh(state: StateStore) -> None:
    secrets = MemorySecretStore()  # shared "keychain" across the two transports
    client = FakeClient()
    t1, _ = _mk(client, state, secrets=secrets)
    await t1.allocate(RuntimeSpec(name="job1"))

    t2, _ = _mk(client, state, secrets=secrets)
    await t2.attach("job1")
    assert client.refresh_calls == 0  # comfortably-unexpired cached token reused


async def test_attach_raises_when_runtime_reclaimed(state: StateStore) -> None:
    client = FakeClient()
    t1, _ = _mk(client, state, secrets=None)
    await t1.allocate(RuntimeSpec(name="job1"))
    client.reclaim(state.get_session("job1").endpoint)  # type: ignore[union-attr]

    t2, _ = _mk(client, state, secrets=None)
    with pytest.raises(RuntimeUnavailableError):
        await t2.attach("job1")


async def test_attach_unknown_name_raises(state: StateStore) -> None:
    t, _ = _mk(FakeClient(), state)
    with pytest.raises(RuntimeUnavailableError):
        await t.attach("nope")


async def test_seconds_until_proxy_expiry_from_record(state: StateStore) -> None:
    client = FakeClient()
    t1, _ = _mk(client, state)
    await t1.allocate(RuntimeSpec(name="job1"))
    # A fresh process reads expiry from the persisted record, not process memory.
    t2, _ = _mk(client, state)
    remaining = t2.seconds_until_proxy_expiry("job1")
    assert remaining is not None and 0 < remaining <= 600


async def test_refresh_token_renews_in_place(state: StateStore) -> None:
    client = FakeClient()
    t, _ = _mk(client, state)
    info = await t.allocate(RuntimeSpec(name="job1"))
    old_token = client.assignments[info.endpoint].runtime_proxy_info.token  # type: ignore[union-attr]

    assert await t.refresh_token("job1") is True
    assert client.refresh_calls == 1  # used the GET-only refresh, not a re-assign
    sess = await t.attach("job1")  # still the same runtime
    assert sess.endpoint == info.endpoint
    # The in-memory session now carries the fresh token.
    assert t._sessions["job1"].assignment.runtime_proxy_info.token != old_token


# -- truthful stop -----------------------------------------------------------


async def test_stop_unknown_name_raises_not_silent(state: StateStore) -> None:
    # The v0.2 bug: stopping an unknown session silently "succeeded" while leaking.
    t, _ = _mk(FakeClient(), state)
    with pytest.raises(RuntimeUnavailableError):
        await t.stop("ghost")


async def test_stop_from_second_process_releases_and_forgets(state: StateStore) -> None:
    client = FakeClient()
    t1, _ = _mk(client, state)
    await t1.allocate(RuntimeSpec(name="job1"))
    endpoint = state.get_session("job1").endpoint  # type: ignore[union-attr]

    t2, _ = _mk(client, state)  # only the record, nothing in memory
    await t2.stop("job1")
    assert endpoint in client.unassigned
    assert state.get_session("job1") is None


async def test_stop_when_already_reclaimed_does_not_raise(state: StateStore) -> None:
    client = FakeClient()
    t1, _ = _mk(client, state)
    await t1.allocate(RuntimeSpec(name="job1"))
    client.reclaim(state.get_session("job1").endpoint)  # type: ignore[union-attr]

    await t1.stop("job1")  # runtime already gone → clean up, don't raise
    assert state.get_session("job1") is None


async def test_stop_by_raw_endpoint_when_live(state: StateStore) -> None:
    client = FakeClient()
    t1, _ = _mk(client, state)
    await t1.allocate(RuntimeSpec(name="job1"))
    endpoint = state.get_session("job1").endpoint  # type: ignore[union-attr]

    t2, _ = _mk(client, state)
    await t2.stop(endpoint)  # caller passes the endpoint itself
    assert endpoint in client.unassigned


# -- reconcile / gc ----------------------------------------------------------


async def test_reconcile_and_gc_release_orphans_and_prune_stale(state: StateStore) -> None:
    client = FakeClient()
    t, _ = _mk(client, state)
    await t.allocate(RuntimeSpec(name="job1"))
    await t.allocate(RuntimeSpec(name="job2"))
    ep1 = state.get_session("job1").endpoint  # type: ignore[union-attr]
    ep2 = state.get_session("job2").endpoint  # type: ignore[union-attr]

    client.reclaim(ep2)  # job2's runtime is gone → record is stale
    client.add_orphan("gpu-orphan")  # server runtime with no local record

    report = await t.reconcile()
    assert report.orphan_endpoints == ["gpu-orphan"]
    assert report.stale_sessions == ["job2"]
    assert ep1 in report.live_tracked

    gc = await t.gc(release_orphans=True)
    assert "gpu-orphan" in client.unassigned  # orphan reclaimed
    assert gc.pruned_records == ["job2"]
    assert {s.name for s in state.list_sessions()} == {"job1"}


async def test_gc_default_is_non_destructive_to_orphans(state: StateStore) -> None:
    client = FakeClient()
    t, _ = _mk(client, state)
    await t.allocate(RuntimeSpec(name="job1"))
    client.add_orphan("gpu-orphan")

    gc = await t.gc()  # default: prune stale only, do NOT release orphans
    assert gc.released_orphans == []
    assert client.unassigned == []
    assert "gpu-orphan" in gc.reconcile.orphan_endpoints  # reported, not touched


# -- is_live probe + bounded keep-alive ping (§5.4 / §5.5) ----------------------


async def test_is_live_true_false_and_unknown(state: StateStore) -> None:
    client = FakeClient()
    t1, _ = _mk(client, state)
    await t1.allocate(RuntimeSpec(name="job1"))
    assert await t1.is_live("job1") is True

    # Cross-process: a fresh transport resolves the endpoint from the record.
    t2, _ = _mk(client, state)
    assert await t2.is_live("job1") is True

    client.reclaim(state.get_session("job1").endpoint)  # type: ignore[union-attr]
    assert await t2.is_live("job1") is False  # gone server-side
    assert await t2.is_live("never-existed") is None  # unresolvable → unknown


async def test_keep_alive_ping_is_time_bounded(state: StateStore) -> None:
    client = FakeClient()
    t, kernels = _mk(client, state)
    await t.allocate(RuntimeSpec(name="job1"))
    await t.keep_alive("job1")
    assert kernels and kernels[0].codes == ["None"]
    (timeout,) = kernels[0].timeouts
    assert timeout is not None and timeout <= 30  # bounded — can't wedge the loop (§5.5)


# -- interrupt (§5.3) + reconnect (§5.6) --------------------------------------


async def test_interrupt_calls_client_with_kernel_id(state: StateStore) -> None:
    client = FakeClient()
    t, _ = _mk(client, state)
    await t.allocate(RuntimeSpec(name="job1"))
    await t.execute("job1", "x=1")  # start the kernel so kernel_id is known
    await t.interrupt("job1")
    assert len(client.interrupts) == 1
    proxy_url, kernel_id, token = client.interrupts[0]
    assert kernel_id == "kid-1"
    assert "tun/m" in proxy_url and token.startswith("ptok")


async def test_reconnect_redials_the_kernel(state: StateStore) -> None:
    client = FakeClient()
    t, kernels = _mk(client, state)
    await t.allocate(RuntimeSpec(name="job1"))
    await t.execute("job1", "x=1")
    await t.reconnect("job1")
    assert kernels[0].reconnected is True
