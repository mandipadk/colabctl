"""Lifecycle tests for NativeColabTransport using fake client + fake kernel (no network)."""

from __future__ import annotations

import base64
from collections.abc import Callable

from colabctl.models import (
    Accelerator,
    Assignment,
    ExecutionResult,
    RuntimeProxyInfo,
    RuntimeSpec,
    SessionStatus,
    StreamOutput,
    Variant,
)
from colabctl.transport.native.adapter import NativeColabTransport


def _assignment(endpoint: str = "gpu-t4-s-abc") -> Assignment:
    return Assignment(
        endpoint=endpoint,
        accelerator=Accelerator.T4,
        variant=Variant.GPU,
        runtime_proxy_info=RuntimeProxyInfo(
            token="ptok", token_expires_in_seconds=600, url=f"https://x/tun/m/{endpoint}"
        ),
    )


class FakeClient:
    def __init__(self) -> None:
        self.assignment = _assignment()
        self.unassigned: list[str] = []
        self.keepalives: list[str] = []
        self.last_accelerator: Accelerator | None = None

    async def assign(self, *, accelerator=Accelerator.T4, notebook_id=None) -> Assignment:
        self.last_accelerator = accelerator
        return self.assignment

    async def list_assignments(self) -> list[Assignment]:
        return [self.assignment]

    async def unassign(self, endpoint: str) -> None:
        self.unassigned.append(endpoint)

    async def keep_alive(self, endpoint: str, *, use_bearer: bool = False) -> None:
        self.keepalives.append(endpoint)


class FakeKernel:
    def __init__(self, responder: Callable[[str], ExecutionResult] | None = None) -> None:
        self.started = False
        self.stopped = False
        self.codes: list[str] = []
        self._responder = responder

    async def start(self) -> None:
        self.started = True

    async def execute(self, code, *, timeout=None, on_output=None) -> ExecutionResult:
        self.codes.append(code)
        result = (
            self._responder(code)
            if self._responder
            else ExecutionResult(status="ok", outputs=[StreamOutput(name="stdout", text="ok")])
        )
        if on_output is not None:
            for o in result.outputs:
                on_output(o)
        return result

    async def restart(self) -> None: ...

    async def stop(self) -> None:
        self.stopped = True


def _transport(responder=None):
    client = FakeClient()
    kernels: list[FakeKernel] = []

    def factory(url: str, token: str) -> FakeKernel:
        k = FakeKernel(responder)
        kernels.append(k)
        return k

    transport = NativeColabTransport(client=client, kernel_factory=factory)  # type: ignore[arg-type]
    return transport, client, kernels


async def test_allocate_records_session():
    transport, client, _ = _transport()
    info = await transport.allocate(RuntimeSpec(accelerator=Accelerator.T4, name="job1"))
    assert info.name == "job1"
    assert info.endpoint == "gpu-t4-s-abc"
    assert info.accelerator is Accelerator.T4
    assert info.status is SessionStatus.IDLE
    assert client.last_accelerator is Accelerator.T4
    assert (await transport.status("job1")) is not None


async def test_execute_lazily_starts_kernel():
    transport, _, kernels = _transport()
    await transport.allocate(RuntimeSpec(name="job1"))
    result = await transport.execute("job1", "print(1)")
    assert result.ok
    assert kernels and kernels[0].started
    assert kernels[0].codes == ["print(1)"]


async def test_upload_checks_sentinel(tmp_path):
    def responder(code: str) -> ExecutionResult:
        ok = "COLABCTL_UPLOAD_OK" in code
        return ExecutionResult(
            status="ok",
            outputs=[StreamOutput(name="stdout", text="COLABCTL_UPLOAD_OK\n" if ok else "")],
        )

    transport, _, _ = _transport(responder)
    await transport.allocate(RuntimeSpec(name="job1"))
    local = tmp_path / "f.txt"
    local.write_text("payload")
    await transport.upload("job1", local, "content/f.txt")  # should not raise


async def test_download_writes_decoded_payload(tmp_path):
    encoded = base64.b64encode(b"remote-bytes").decode()

    def responder(code: str) -> ExecutionResult:
        text = f"<<<COLABCTL_B64>>>{encoded}<<<COLABCTL_END>>>\n"
        return ExecutionResult(status="ok", outputs=[StreamOutput(name="stdout", text=text)])

    transport, _, _ = _transport(responder)
    await transport.allocate(RuntimeSpec(name="job1"))
    dest = tmp_path / "out.bin"
    await transport.download("job1", "content/x.bin", dest)
    assert dest.read_bytes() == b"remote-bytes"


async def test_stop_unassigns_and_stops_kernel():
    transport, client, kernels = _transport()
    await transport.allocate(RuntimeSpec(name="job1"))
    await transport.execute("job1", "x=1")  # forces kernel creation
    await transport.stop("job1")
    assert client.unassigned == ["gpu-t4-s-abc"]
    assert kernels[0].stopped
    assert (await transport.status("job1")) is None


async def test_keep_alive_pings_kernel():
    # The RuntimeService RPC is unusable under token auth (live-confirmed), so
    # keep_alive registers kernel activity instead of calling the RPC.
    transport, client, kernels = _transport()
    await transport.allocate(RuntimeSpec(name="job1"))
    await transport.keep_alive("job1")
    assert kernels and kernels[0].started
    assert "None" in kernels[0].codes
    assert client.keepalives == []  # RPC keep-alive no longer used


async def test_seconds_until_proxy_expiry():
    transport, _, _ = _transport()
    await transport.allocate(RuntimeSpec(name="job1"))
    remaining = transport.seconds_until_proxy_expiry("job1")
    assert remaining is not None
    assert 0 < remaining <= 600  # token_expires_in_seconds is 600 in the fake
    assert transport.seconds_until_proxy_expiry("missing") is None


async def test_list_sessions_maps_known_names():
    transport, _, _ = _transport()
    await transport.allocate(RuntimeSpec(name="job1"))
    sessions = await transport.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].name == "job1"
    assert sessions[0].endpoint == "gpu-t4-s-abc"
