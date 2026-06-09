"""Offline tests for kernel output normalization + file-transfer code helpers."""

from __future__ import annotations

import base64

import pytest

from colabctl.errors import FileTransferError
from colabctl.models import DisplayDataOutput, ErrorOutput, ExecuteResultOutput, StreamOutput
from colabctl.transport.native import kernel


def test_normalize_stream_joins_list_text():
    out = kernel.normalize_output({"output_type": "stream", "name": "stdout", "text": ["a", "b"]})
    assert isinstance(out, StreamOutput)
    assert out.name == "stdout"
    assert out.text == "ab"


def test_normalize_stderr():
    out = kernel.normalize_output({"output_type": "stream", "name": "stderr", "text": "oops"})
    assert isinstance(out, StreamOutput)
    assert out.name == "stderr"


def test_normalize_execute_result():
    out = kernel.normalize_output(
        {"output_type": "execute_result", "data": {"text/plain": "42"}, "execution_count": 3}
    )
    assert isinstance(out, ExecuteResultOutput)
    assert out.data["text/plain"] == "42"
    assert out.execution_count == 3


def test_normalize_display_data():
    out = kernel.normalize_output({"output_type": "display_data", "data": {"image/png": "b64..."}})
    assert isinstance(out, DisplayDataOutput)
    assert "image/png" in out.data


def test_normalize_error():
    out = kernel.normalize_output(
        {"output_type": "error", "ename": "ValueError", "evalue": "x", "traceback": ["t1"]}
    )
    assert isinstance(out, ErrorOutput)
    assert out.ename == "ValueError"
    assert out.traceback == ["t1"]


def test_normalize_unknown_returns_none():
    assert kernel.normalize_output({"output_type": "weird"}) is None


def test_outputs_to_result_derives_error_status():
    result = kernel.outputs_to_result(
        {"outputs": [{"output_type": "error", "ename": "E", "evalue": "v", "traceback": []}]}
    )
    assert result.status == "error"
    assert not result.ok


def test_outputs_to_result_ok_with_stream():
    result = kernel.outputs_to_result(
        {
            "status": "ok",
            "execution_count": 1,
            "outputs": [{"output_type": "stream", "name": "stdout", "text": "hi"}],
        }
    )
    assert result.ok
    assert result.execution_count == 1
    assert result.stdout == "hi"


def test_upload_code_embeds_path_and_data():
    code = kernel.build_upload_code("content/x.txt", "ZGF0YQ==")
    assert "content/x.txt" in code
    assert "ZGF0YQ==" in code
    assert "COLABCTL_UPLOAD_OK" in code


def test_download_code_and_payload_roundtrip():
    code = kernel.build_download_code("content/x.bin")
    assert "content/x.bin" in code
    # Simulate the VM's printed output:
    encoded = base64.b64encode(b"hello bytes").decode()
    printed = f"<<<COLABCTL_B64>>>{encoded}<<<COLABCTL_END>>>\n"
    assert kernel.parse_b64_payload(printed) == b"hello bytes"


def test_parse_b64_payload_missing_markers_raises():
    with pytest.raises(FileTransferError):
        kernel.parse_b64_payload("no markers here")
