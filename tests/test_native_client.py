"""Offline tests for the native /tun/m/* client's verified pure helpers.

No network: these lock in the wire-contract details (nbh encoding, XSSI prefix,
assign params, the public API key, and the keep-alive header recipe that fixes
the ADC 403) so a regression is caught without a live Colab account.
"""

from __future__ import annotations

import uuid

from colabctl.models import Accelerator, Variant
from colabctl.transport.native import client


def test_web_safe_nbh_encoding():
    nb = uuid.UUID("12345678-1234-5678-1234-567812345678")
    nbh = client.web_safe_nbh(nb)
    assert nbh == "12345678_1234_5678_1234_567812345678........"
    assert len(nbh) == 44
    assert "-" not in nbh


def test_strip_xssi():
    assert client.strip_xssi(')]}\'\n{"a": 1}') == '{"a": 1}'
    assert client.strip_xssi('{"a": 1}') == '{"a": 1}'


def test_build_assign_params():
    params = client.build_assign_params("NBH", variant=Variant.GPU, accelerator=Accelerator.T4)
    assert params == {"nbh": "NBH", "variant": "GPU", "accelerator": "T4"}


def test_build_assign_params_cpu_only_nbh():
    assert client.build_assign_params("NBH") == {"nbh": "NBH"}


def test_public_api_key_decode():
    # Public web-client key — auditable, not a secret.
    assert client.PUBLIC_API_KEY_HEADER == "x-goog-api-key"
    assert client.PUBLIC_API_KEY == "AIzaSyA2BvntLwNwFthUB4w6_Bhn0cMlVHwyaHc"


def test_keepalive_headers_are_api_key_only_by_default():
    headers = client.keepalive_headers()
    # The fix: keep-alive must NOT carry an OAuth bearer (avoids serviceusage 403).
    assert "Authorization" not in headers
    assert headers[client.PUBLIC_API_KEY_HEADER] == client.PUBLIC_API_KEY
    assert headers[client.USER_PROJECT_HEADER] == client.COLAB_PROJECT_ID
    assert headers[client.API_CLIENT_HEADER] == "grpc-web/0.1"


def test_keepalive_headers_can_drop_user_project():
    headers = client.keepalive_headers(pin_colab_project=False)
    assert client.USER_PROJECT_HEADER not in headers


def test_proxy_kernel_headers_and_ws_params():
    headers = client.ColabBackendClient.proxy_kernel_headers("PTOK")
    assert headers[client.PROXY_TOKEN_HEADER] == "PTOK"
    assert headers[client.CLIENT_AGENT_HEADER] == client.CLIENT_AGENT
    params = client.ColabBackendClient.proxy_ws_params("PTOK")
    assert params == {"colab-runtime-proxy-token": "PTOK"}


def test_assignment_from_wire_maps_integer_variant():
    # The backend sends variant as an int (GPU=1) and machineShape as an int.
    a = client.assignment_from_wire(
        {
            "endpoint": "gpu-t4-s-abc",
            "accelerator": "T4",
            "variant": 1,
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
    assert a.machine_shape.value == 1
    assert a.runtime_proxy_info is not None
    assert a.runtime_proxy_info.token == "ptok"


def test_assignment_from_wire_existing_assignment_minimal():
    a = client.assignment_from_wire(
        {
            "endpoint": "gpu-t4-s-xyz",
            "runtimeProxyInfo": {
                "token": "t",
                "tokenExpiresInSeconds": 60,
                "url": "https://x/tun/m/gpu-t4-s-xyz",
            },
        }
    )
    assert a.endpoint == "gpu-t4-s-xyz"
    assert a.accelerator is Accelerator.NONE  # absent → CPU/NONE
    assert a.variant is Variant.DEFAULT


def test_coerce_variant_handles_unknowns():
    assert client._coerce_variant(2) is Variant.TPU
    assert client._coerce_variant("GPU") is Variant.GPU
    assert client._coerce_variant(99) is Variant.DEFAULT
    assert client._coerce_variant(None) is Variant.DEFAULT
