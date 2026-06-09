"""Adversarial tests for MCP accelerator coercion + JSON-serialization shape."""

from __future__ import annotations

import json

import pytest

from colabctl.errors import ConfigurationError
from colabctl.mcp_server import _accelerator, _result_dict, _session_dict
from colabctl.models import (
    Accelerator,
    ErrorOutput,
    ExecutionResult,
    SessionInfo,
    SessionStatus,
    StreamOutput,
    Variant,
)


@pytest.mark.parametrize(
    "gpu,expected",
    [
        ("none", Accelerator.NONE),
        ("NONE", Accelerator.NONE),
        ("None", Accelerator.NONE),
        ("t4", Accelerator.T4),
        ("T4", Accelerator.T4),
        ("a100", Accelerator.A100),
        ("h100", Accelerator.H100),
    ],
)
def test_accelerator_coercion(gpu, expected):
    assert _accelerator(gpu) is expected


@pytest.mark.parametrize("gpu", ["cpu", "", "tpu", "xyz", "rtx4090", "  t4  "])
def test_accelerator_invalid_raises(gpu):
    with pytest.raises(ConfigurationError):
        _accelerator(gpu)


def test_result_dict_ok_shape_is_json_serializable():
    r = ExecutionResult(
        status="ok",
        outputs=[
            StreamOutput(name="stdout", text="hi"),
            StreamOutput(name="stderr", text="warn"),
        ],
    )
    d = _result_dict(r)
    assert d["ok"] is True
    assert d["status"] == "ok"
    assert d["stdout"] == "hi"
    assert d["stderr"] == "warn"
    assert d["error"] is None
    json.dumps(d)  # must serialize cleanly to the agent


def test_result_dict_error_shape():
    r = ExecutionResult(
        status="error",
        outputs=[ErrorOutput(ename="ValueError", evalue="boom", traceback=["a", "b"])],
    )
    d = _result_dict(r)
    assert d["ok"] is False
    assert d["error"] == {"ename": "ValueError", "evalue": "boom", "traceback": ["a", "b"]}
    json.dumps(d)


def test_session_dict_shape_and_cpu_label():
    info = SessionInfo(
        name="s1",
        endpoint="ep",
        accelerator=Accelerator.NONE,
        variant=Variant.DEFAULT,
        status=SessionStatus.IDLE,
    )
    d = _session_dict(info)
    assert d == {
        "name": "s1",
        "endpoint": "ep",
        "accelerator": "NONE",
        "hardware": "CPU",  # NONE renders as CPU for humans/agents
        "variant": "DEFAULT",
        "status": "IDLE",
    }
    json.dumps(d)


def test_session_dict_gpu_label():
    info = SessionInfo(
        name="s",
        endpoint="ep",
        accelerator=Accelerator.A100,
        variant=Variant.GPU,
        status=SessionStatus.BUSY,
    )
    d = _session_dict(info)
    assert d["hardware"] == "A100"
    assert d["accelerator"] == "A100"
