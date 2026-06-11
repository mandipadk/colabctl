"""Kernel-level ops: output cap (§5.9), kernel_id exposure + reconnect (§5.6)."""

from __future__ import annotations

import pytest

from colabctl.errors import KernelError
from colabctl.models import ErrorOutput, ExecutionResult, StreamOutput
from colabctl.transport.native.kernel import NativeKernel, cap_stream_output

# -- output cap (pure) --------------------------------------------------------


def _stream(text: str, name: str = "stdout") -> ExecutionResult:
    return ExecutionResult(status="ok", outputs=[StreamOutput(name=name, text=text)])


def test_cap_under_limit_is_unchanged() -> None:
    r = _stream("hello")
    assert cap_stream_output(r, 100) is r


def test_cap_zero_disables_capping() -> None:
    r = _stream("A" * 10_000)
    assert cap_stream_output(r, 0) is r


def test_cap_truncates_head_and_tail_with_marker() -> None:
    r = _stream("A" * 5000 + "B" * 5000)
    out = cap_stream_output(r, 100)
    text = out.stdout
    assert "truncated" in text
    assert len(text) < 10_000
    assert text.startswith("A")  # head kept
    assert text.rstrip().endswith("B")  # tail kept


def test_cap_preserves_non_stream_outputs() -> None:
    r = ExecutionResult(
        status="error",
        outputs=[
            StreamOutput(name="stdout", text="A" * 1000),
            ErrorOutput(ename="ValueError", evalue="boom"),
        ],
    )
    out = cap_stream_output(r, 50)
    assert any(isinstance(o, ErrorOutput) for o in out.outputs)
    assert out.error is not None and out.error.ename == "ValueError"


def test_cap_merges_multiple_streams_into_one() -> None:
    r = ExecutionResult(
        status="ok",
        outputs=[
            StreamOutput(name="stdout", text="A" * 600),
            StreamOutput(name="stderr", text="B" * 600),
        ],
    )
    out = cap_stream_output(r, 100)
    assert sum(isinstance(o, StreamOutput) for o in out.outputs) == 1


# -- kernel_id + reconnect ----------------------------------------------------


class _StubKernel(NativeKernel):
    """NativeKernel with the jupyter-kernel-client sync calls stubbed out."""

    def __init__(self, **kw) -> None:
        super().__init__("https://proxy/tun/m/ep", "ptok", **kw)
        self.built = 0
        self.stopped = 0

    def _build_and_start(self):  # type: ignore[override]
        self.built += 1
        if self._kernel_id is None:
            self._kernel_id = "kid-built"
        return object()

    def _stop_sync(self) -> None:  # type: ignore[override]
        self.stopped += 1


def test_kernel_id_is_exposed() -> None:
    assert _StubKernel(kernel_id="kid-x").kernel_id == "kid-x"


async def test_reconnect_rebuilds_to_same_kernel() -> None:
    k = _StubKernel(kernel_id="kid-x")
    await k.start()
    assert k.built == 1
    await k.reconnect()
    assert k.built == 2 and k.stopped == 1  # tore down + rebuilt
    assert k.kernel_id == "kid-x"  # same server-side kernel


async def test_reconnect_without_kernel_id_raises() -> None:
    k = _StubKernel()  # never started → no id retained
    with pytest.raises(KernelError):
        await k.reconnect()
