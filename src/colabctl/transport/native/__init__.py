"""The native, from-scratch Colab transport (co-primary, opt-in).

This package owns colabctl's own implementation of the Colab backend protocol —
the ``/tun/m/*`` assignment REST flow (:mod:`client`), the Jupyter-websocket
kernel client (:mod:`kernel`), and the full transport (:mod:`adapter`) — so the
product is never hostage to the
immature official CLI (project directive; see ``DIRECTIVES.md``). The recipe was
verified from Apache-2.0 CLI source in Phase 0 (``spikes/PHASE0-FINDINGS.md`` §3).

It is **disabled by default** per the sanctioned-default ToS posture; callers opt
in explicitly. On keep-alive: live testing (PHASE0-FINDINGS §2) confirmed the
RuntimeService keep-alive RPC is unusable from token auth (401 api-key / 403 bearer)
— only the browser's session cookies work — so this transport keeps runtimes alive
via kernel activity and relies on checkpoint/re-assign for long jobs.
"""

from __future__ import annotations

from colabctl.transport.native.adapter import (
    GcReport,
    NativeColabTransport,
    ReconcileReport,
)
from colabctl.transport.native.client import (
    COLAB_API_DOMAIN,
    COLAB_DOMAIN,
    PUBLIC_API_KEY,
    PUBLIC_API_KEY_HEADER,
    ColabBackendClient,
    build_assign_params,
    strip_xssi,
    web_safe_nbh,
)
from colabctl.transport.native.kernel import (
    NativeKernel,
    normalize_output,
    outputs_to_result,
)

__all__ = [
    "COLAB_API_DOMAIN",
    "COLAB_DOMAIN",
    "PUBLIC_API_KEY",
    "PUBLIC_API_KEY_HEADER",
    "ColabBackendClient",
    "GcReport",
    "NativeColabTransport",
    "NativeKernel",
    "ReconcileReport",
    "build_assign_params",
    "normalize_output",
    "outputs_to_result",
    "strip_xssi",
    "web_safe_nbh",
]
