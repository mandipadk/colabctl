"""Tests for the domain models: enum helpers, aliases, and ExecutionResult views."""

from __future__ import annotations

from colabctl.models import (
    Accelerator,
    Assignment,
    ErrorOutput,
    ExecutionResult,
    MachineShape,
    RuntimeProxyInfo,
    RuntimeSpec,
    StreamOutput,
    Variant,
)


def test_accelerator_helpers():
    assert Accelerator.T4.is_gpu
    assert not Accelerator.T4.is_tpu
    assert Accelerator.T4.label == "T4"
    assert Accelerator.NONE.label == "CPU"
    assert Accelerator.V5E1.is_tpu
    assert not Accelerator.V5E1.is_gpu


def test_variant_for_accelerator():
    assert Variant.for_accelerator(Accelerator.T4) is Variant.GPU
    assert Variant.for_accelerator(Accelerator.V6E1) is Variant.TPU
    assert Variant.for_accelerator(Accelerator.NONE) is Variant.DEFAULT


def test_runtime_spec_variant():
    assert RuntimeSpec(accelerator=Accelerator.A100).variant is Variant.GPU
    assert RuntimeSpec(accelerator=Accelerator.NONE).variant is Variant.DEFAULT


def test_runtime_proxy_info_alias():
    info = RuntimeProxyInfo.model_validate(
        {"token": "t", "tokenExpiresInSeconds": 60, "url": "https://x/tun/m/ep"}
    )
    assert info.token_expires_in_seconds == 60


def test_assignment_aliases():
    a = Assignment.model_validate(
        {
            "endpoint": "gpu-t4-s-abc",
            "accelerator": "T4",
            "variant": "GPU",
            "machineShape": 1,
            "runtimeProxyInfo": {
                "token": "ptok",
                "tokenExpiresInSeconds": 600,
                "url": "https://x/tun/m/gpu-t4-s-abc",
            },
        }
    )
    assert a.accelerator is Accelerator.T4
    assert a.variant is Variant.GPU
    assert a.machine_shape is MachineShape.HIGH_RAM
    assert a.runtime_proxy_info is not None
    assert a.runtime_proxy_info.token == "ptok"


def test_execution_result_ok_and_streams():
    r = ExecutionResult(
        status="ok",
        outputs=[
            StreamOutput(name="stdout", text="hello\n"),
            StreamOutput(name="stderr", text="warn\n"),
        ],
    )
    assert r.ok
    assert r.stdout == "hello\n"
    assert r.stderr == "warn\n"
    assert r.error is None
    assert "hello" in r.text


def test_execution_result_error_view():
    r = ExecutionResult(
        status="error",
        outputs=[ErrorOutput(ename="ValueError", evalue="boom", traceback=["line1"])],
    )
    assert not r.ok
    assert r.error is not None
    assert r.error.ename == "ValueError"
