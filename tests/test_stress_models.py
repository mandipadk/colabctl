"""Contract + edge tests for the domain models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from colabctl.models import (
    Accelerator,
    Assignment,
    ErrorOutput,
    ExecuteResultOutput,
    ExecutionResult,
    MachineShape,
    RuntimeSpec,
    StreamOutput,
    Variant,
)

# --- enum classification is exhaustive & disjoint ---------------------------


def test_every_accelerator_is_gpu_xor_tpu_xor_none():
    for a in Accelerator:
        kinds = [a.is_gpu, a.is_tpu, a is Accelerator.NONE]
        assert sum(kinds) == 1, f"{a} classified as {kinds}"


def test_accelerator_label():
    assert Accelerator.NONE.label == "CPU"
    assert Accelerator.T4.label == "T4"


@pytest.mark.parametrize(
    "acc,variant",
    [
        (Accelerator.NONE, Variant.DEFAULT),
        (Accelerator.T4, Variant.GPU),
        (Accelerator.A100, Variant.GPU),
        (Accelerator.V5E1, Variant.TPU),
        (Accelerator.V6E1, Variant.TPU),
    ],
)
def test_variant_for_accelerator(acc, variant):
    assert Variant.for_accelerator(acc) is variant
    assert RuntimeSpec(accelerator=acc).variant is variant


# --- ExecutionResult aggregation --------------------------------------------


def test_stdout_stderr_split_and_interleaved():
    r = ExecutionResult(
        outputs=[
            StreamOutput(name="stdout", text="out1\n"),
            StreamOutput(name="stderr", text="err1\n"),
            StreamOutput(name="stdout", text="out2\n"),
        ]
    )
    assert r.stdout == "out1\nout2\n"
    assert r.stderr == "err1\n"


def test_error_property_returns_first_error():
    r = ExecutionResult(
        status="error",
        outputs=[
            StreamOutput(name="stdout", text="x"),
            ErrorOutput(ename="ValueError", evalue="bad", traceback=["t"]),
            ErrorOutput(ename="KeyError", evalue="second"),
        ],
    )
    assert not r.ok
    assert r.error.ename == "ValueError"


def test_text_includes_stream_and_text_plain_forms():
    r = ExecutionResult(
        outputs=[
            StreamOutput(name="stdout", text="A"),
            ExecuteResultOutput(data={"text/plain": "B"}),
            ExecuteResultOutput(data={"text/plain": ["C", "D"]}),
            ExecuteResultOutput(data={"image/png": "ignored"}),
            ExecuteResultOutput(data={"text/plain": {"weird": 1}}),  # non-str/list → skipped
        ]
    )
    assert r.text == "ABCD"


def test_empty_result_is_ok_with_blank_text():
    r = ExecutionResult()
    assert r.ok and r.text == "" and r.stdout == "" and r.error is None


# --- discriminated Output union ---------------------------------------------


def test_output_union_parses_by_discriminator():
    r = ExecutionResult.model_validate(
        {"status": "ok", "outputs": [{"output_type": "stream", "name": "stdout", "text": "hi"}]}
    )
    assert isinstance(r.outputs[0], StreamOutput)


def test_output_union_rejects_unknown_type():
    with pytest.raises(ValidationError):
        ExecutionResult.model_validate(
            {"outputs": [{"output_type": "totally_unknown", "text": "x"}]}
        )


def test_stream_output_rejects_bad_name():
    with pytest.raises(ValidationError):
        StreamOutput(name="stdlog", text="x")  # only stdout/stderr allowed


def test_invalid_execution_status_rejected():
    with pytest.raises(ValidationError):
        ExecutionResult(status="weird")


# --- Assignment wire parsing (camelCase aliases) ----------------------------


def test_assignment_from_camelcase_wire():
    a = Assignment.model_validate(
        {
            "endpoint": "ep",
            "accelerator": "A100",
            "variant": "GPU",
            "machineShape": 1,
            "runtimeProxyInfo": {
                "token": "tok",
                "tokenExpiresInSeconds": 3600,
                "url": "https://x",
            },
        }
    )
    assert a.machine_shape is MachineShape.HIGH_RAM
    assert a.runtime_proxy_info.token_expires_in_seconds == 3600
    assert a.accelerator is Accelerator.A100


def test_assignment_defaults_when_minimal():
    a = Assignment.model_validate({"endpoint": "ep"})
    assert a.accelerator is Accelerator.NONE
    assert a.variant is Variant.DEFAULT
    assert a.machine_shape is MachineShape.STANDARD
    assert a.runtime_proxy_info is None
