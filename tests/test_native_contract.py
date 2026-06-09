"""Contract/drift guard for the verified native /tun/m/* recipe.

These assertions pin the exact values reverse-engineered + cross-confirmed in Phase 0
(from google-colab-cli + colab-mcp source). If any of them changes accidentally, this
breaks loudly — the reverse-engineered contract must never drift silently.
See spikes/PHASE0-FINDINGS.md §3.
"""

from __future__ import annotations

import uuid

from colabctl.models import Accelerator, Variant
from colabctl.transport.native import client as c


def test_endpoints_and_hosts():
    assert c.COLAB_DOMAIN == "https://colab.research.google.com"
    assert c.COLAB_API_DOMAIN == "https://colab.pa.googleapis.com"
    assert c.TUN_ENDPOINT == "/tun/m"
    assert c.ASSIGN_PATH == "/tun/m/assign"
    assert c.ASSIGNMENTS_PATH == "/tun/m/assignments"
    assert c.UNASSIGN_PATH == "/tun/m/unassign"
    assert c.KEEPALIVE_RPC == ("/$rpc/google.internal.colab.v1.RuntimeService/KeepAliveAssignment")


def test_headers_and_guards():
    assert c.XSSI_PREFIX == ")]}'\n"
    assert c.XSRF_HEADER == "X-Goog-Colab-Token"
    assert c.PROXY_TOKEN_HEADER == "X-Colab-Runtime-Proxy-Token"
    assert c.CLIENT_AGENT_HEADER == "X-Colab-Client-Agent"
    assert c.USER_PROJECT_HEADER == "x-goog-user-project"
    assert c.COLAB_PROJECT_ID == "1014160490159"


def test_public_api_key_decodes_to_known_value():
    assert c.PUBLIC_API_KEY_HEADER == "x-goog-api-key"
    assert c.PUBLIC_API_KEY == "AIzaSyA2BvntLwNwFthUB4w6_Bhn0cMlVHwyaHc"


def test_accelerator_enum_values():
    assert {a.value for a in Accelerator} == {
        "NONE",
        "T4",
        "L4",
        "G4",
        "A100",
        "H100",
        "V5E1",
        "V6E1",
    }


def test_variant_int_mapping():
    assert c._VARIANT_BY_INT == {0: Variant.DEFAULT, 1: Variant.GPU, 2: Variant.TPU}


def test_nbh_encoding_is_44_chars_web_safe():
    nbh = c.web_safe_nbh(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    assert len(nbh) == 44
    assert "-" not in nbh
    assert nbh.startswith("12345678_1234_5678_1234_567812345678")


def test_kernel_ws_auth_recipe():
    # Header-only proxy token + the ws query param (the corrected recipe).
    headers = c.ColabBackendClient.proxy_kernel_headers("PTOK")
    assert headers == {
        c.CLIENT_AGENT_HEADER: c.CLIENT_AGENT,
        c.PROXY_TOKEN_HEADER: "PTOK",
    }
    assert c.ColabBackendClient.proxy_ws_params("PTOK") == {"colab-runtime-proxy-token": "PTOK"}


def test_keepalive_headers_recipe():
    headers = c.keepalive_headers()
    assert headers[c.PUBLIC_API_KEY_HEADER] == c.PUBLIC_API_KEY
    assert headers[c.USER_PROJECT_HEADER] == c.COLAB_PROJECT_ID
    assert headers[c.API_CLIENT_HEADER] == "grpc-web/0.1"
    assert "Authorization" not in headers
