"""Adversarial + property tests for the native client + kernel pure helpers."""

from __future__ import annotations

import base64
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from colabctl.errors import FileTransferError
from colabctl.models import Accelerator, MachineShape, Variant
from colabctl.transport.native import client as c
from colabctl.transport.native import kernel as k

# --- web_safe_nbh ----------------------------------------------------------


@given(u=st.uuids())
def test_web_safe_nbh_is_always_44_chars(u):
    nbh = c.web_safe_nbh(u)
    assert len(nbh) == 44
    assert "-" not in nbh
    assert nbh[:36] == str(u).replace("-", "_")
    assert set(nbh[36:]) <= {"."}


# --- strip_xssi ------------------------------------------------------------


def test_strip_xssi_edge_cases():
    assert c.strip_xssi(")]}'\n{}") == "{}"
    assert c.strip_xssi("{}") == "{}"
    assert c.strip_xssi("") == ""
    # only a leading, exact prefix is stripped
    assert c.strip_xssi(")]}'") == ")]}'"  # missing newline → not the prefix
    assert c.strip_xssi("x)]}'\n") == "x)]}'\n"  # not leading
    assert c.strip_xssi(")]}'\n)]}'\n") == ")]}'\n"  # only once


@given(body=st.text())
def test_strip_xssi_roundtrip(body):
    assert c.strip_xssi(c.XSSI_PREFIX + body) == body


# --- variant / accelerator coercion ----------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, Variant.DEFAULT),
        (1, Variant.GPU),
        (2, Variant.TPU),
        (99, Variant.DEFAULT),
        (-1, Variant.DEFAULT),
        ("GPU", Variant.GPU),
        ("gpu", Variant.DEFAULT),  # values are upper-case
        ("nonsense", Variant.DEFAULT),
        (None, Variant.DEFAULT),
        (True, Variant.DEFAULT),
        (1.5, Variant.DEFAULT),
    ],
)
def test_coerce_variant(raw, expected):
    assert c._coerce_variant(raw) is expected


def test_assignment_from_wire_missing_and_odd_fields():
    a = c.assignment_from_wire({"endpoint": "ep"})  # only endpoint
    assert a.endpoint == "ep"
    assert a.accelerator is Accelerator.NONE
    assert a.variant is Variant.DEFAULT
    assert a.machine_shape is MachineShape.STANDARD
    assert a.runtime_proxy_info is None

    a2 = c.assignment_from_wire(
        {"endpoint": "ep", "accelerator": "nope", "variant": 1, "machineShape": "weird"}
    )
    assert a2.accelerator is Accelerator.NONE  # unknown accelerator → NONE
    assert a2.variant is Variant.GPU
    assert a2.machine_shape is MachineShape.STANDARD  # non-int shape → default


# --- normalize_output ------------------------------------------------------


def test_normalize_output_text_forms_and_missing_fields():
    assert k.normalize_output({"output_type": "stream", "text": ["a", "b"]}).text == "ab"
    assert k.normalize_output({"output_type": "stream", "text": 5}).text == "5"
    assert k.normalize_output({"output_type": "stream"}).text == ""  # missing text
    assert k.normalize_output({"output_type": "stream"}).name == "stdout"  # default
    assert k.normalize_output({"output_type": "execute_result"}).data == {}
    assert k.normalize_output({"output_type": "error"}).traceback == []
    assert k.normalize_output({"output_type": "weird"}) is None
    assert k.normalize_output({}) is None  # no output_type


def test_outputs_to_result_status_derivation():
    assert k.outputs_to_result({"outputs": []}).status == "ok"
    assert k.outputs_to_result({"outputs": [{"output_type": "error"}]}).status == "error"
    # explicit status wins
    assert k.outputs_to_result({"status": "abort", "outputs": []}).status == "abort"
    # bogus status falls back to derivation
    assert k.outputs_to_result({"status": "???", "outputs": []}).status == "ok"


# --- file-transfer code builders (must always produce valid python) --------


@given(path=st.text(), data=st.text())
def test_upload_code_always_compiles(path, data):
    compile(k.build_upload_code(path, data), "<gen>", "exec")


@given(path=st.text())
def test_download_code_always_compiles(path):
    compile(k.build_download_code(path), "<gen>", "exec")


@given(payload=st.binary())
def test_b64_payload_roundtrip(payload):
    encoded = base64.b64encode(payload).decode()
    text = f"noise {k._B64_BEGIN}{encoded}{k._B64_END} trailing"
    assert k.parse_b64_payload(text) == payload


def test_parse_b64_payload_failures():
    with pytest.raises(FileTransferError):
        k.parse_b64_payload("no markers")
    with pytest.raises(FileTransferError):
        k.parse_b64_payload(k._B64_BEGIN + "no end marker")
    with pytest.raises(FileTransferError):
        k.parse_b64_payload(f"{k._B64_BEGIN}!!!not base64!!!{k._B64_END}")


def test_nbh_for_standard_uuid_is_stable():
    nbh = c.web_safe_nbh(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    assert nbh == "12345678_1234_5678_1234_567812345678........"
