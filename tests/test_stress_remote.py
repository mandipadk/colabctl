"""Adversarial + round-trip tests for the @remote marshalling helpers."""

from __future__ import annotations

import base64

import cloudpickle
import pytest
from hypothesis import given
from hypothesis import strategies as st

# Import names directly from the submodule path: the `colabctl.sdk` package
# re-exports the `remote` *function*, which shadows the `remote` *module* for
# `from colabctl.sdk import remote`.
from colabctl.errors import SerializationError
from colabctl.sdk.remote import (
    RESULT_BEGIN,
    RESULT_END,
    build_remote_harness,
    decode_result,
    encode_call,
    parse_result_payload,
    remote,
)


def _simulate_kernel_output(value: object, *, noise: str = "") -> str:
    """Build what the VM harness would print for a given return value."""
    enc = base64.b64encode(cloudpickle.dumps(value)).decode()
    return f"{noise}some stdout\n{RESULT_BEGIN}{enc}{RESULT_END}\n"


# --- harness code generation -------------------------------------------------


@given(payload=st.text())
def test_harness_always_compiles(payload):
    b64 = base64.b64encode(payload.encode()).decode()
    compile(build_remote_harness(b64), "<harness>", "exec")


def test_markers_cannot_appear_in_base64():
    # base64 alphabet excludes '<', so a payload region can never contain a marker.
    assert "<" not in (base64.b64encode(b"\x00\xff" * 100).decode())


# --- full offline round-trip -------------------------------------------------


def test_encode_decode_roundtrip_simple():
    def fn(a, b, *, c):
        return a + b + c

    payload = encode_call(fn, (1, 2), {"c": 3})
    rfn, rargs, rkwargs = cloudpickle.loads(base64.b64decode(payload))
    assert rfn(*rargs, **rkwargs) == 6
    # and the result decodes back through the marker frame
    out = _simulate_kernel_output(rfn(*rargs, **rkwargs))
    assert decode_result(out) == 6


def test_roundtrip_closure_over_local():
    factor = 10

    def scale(x):
        return x * factor

    payload = encode_call(scale, (5,), {})
    rfn, rargs, rkwargs = cloudpickle.loads(base64.b64decode(payload))
    assert rfn(*rargs, **rkwargs) == 50


@given(
    value=st.one_of(
        st.integers(),
        st.text(),
        st.lists(st.integers()),
        st.dictionaries(st.text(), st.integers()),
    )
)
def test_decode_result_roundtrips_arbitrary_values(value):
    assert decode_result(_simulate_kernel_output(value)) == value


def test_decode_result_tolerates_trailing_noise_and_newlines():
    out = _simulate_kernel_output([1, 2, 3], noise="warning: blah\n")
    assert decode_result(out) == [1, 2, 3]


# --- parse failures ----------------------------------------------------------


def test_parse_result_payload_no_markers():
    with pytest.raises(SerializationError):
        parse_result_payload("nothing here")


def test_parse_result_payload_only_begin():
    with pytest.raises(SerializationError):
        parse_result_payload(RESULT_BEGIN + "abc")


def test_parse_result_payload_corrupt_base64():
    with pytest.raises(SerializationError):
        parse_result_payload(f"{RESULT_BEGIN}!!!not-b64!!!{RESULT_END}")


def test_encode_call_rejects_unpicklable():
    import threading

    lock = threading.Lock()  # not picklable
    with pytest.raises(SerializationError):
        encode_call(lambda: lock, (lock,), {})


# --- decorator wiring (no network) -------------------------------------------


def test_decorator_bare_form_preserves_metadata():
    @remote
    def my_fn(x):
        """docstring."""
        return x

    assert my_fn.__name__ == "my_fn"
    assert "docstring" in (my_fn.__doc__ or "")
    assert callable(my_fn.aio)


def test_decorator_parametrized_form_returns_callable():
    @remote(gpu="A100", keep=True)
    def my_fn(x):
        return x

    assert callable(my_fn)
    assert callable(my_fn.aio)
