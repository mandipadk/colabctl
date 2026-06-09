"""Backend construction by name — the seam the CLI and MCP server build on.

Keeps backend wiring in one place so ``colabctl job run --backend modal`` and the
MCP ``run_job`` tool construct backends identically. The Colab backend is built over
the chosen transport (cli/native); Modal/Vertex read their own env/config.
"""

from __future__ import annotations

from colabctl.backends.base import Backend
from colabctl.backends.modal_backend import ModalBackend
from colabctl.backends.router import BackendRouter
from colabctl.backends.vertex_backend import VertexBackend
from colabctl.errors import ConfigurationError

#: Backends available for selection.
BACKEND_NAMES: tuple[str, ...] = ("colab", "modal", "vertex", "hf", "kaggle", "runpod")


def build_backend(
    name: str,
    *,
    transport_name: str = "cli",
    auth_mode: str = "adc",
    colab_bin: str = "colab",
) -> Backend:
    """Construct a backend by name. Colab uses the chosen transport."""
    key = name.lower()
    if key == "colab":
        from colabctl.backends.colab import ColabBackend
        from colabctl.sdk.client import ColabClient

        client = ColabClient(
            transport_name=transport_name, auth_mode=auth_mode, colab_bin=colab_bin
        )
        return ColabBackend(client.transport)
    if key == "modal":
        return ModalBackend()
    if key == "vertex":
        return VertexBackend()
    if key == "hf":
        from colabctl.backends.hf_backend import HFJobsBackend

        return HFJobsBackend()
    if key == "kaggle":
        from colabctl.backends.kaggle_backend import KaggleBackend

        return KaggleBackend()
    if key == "runpod":
        from colabctl.backends.runpod_backend import RunPodBackend

        return RunPodBackend()
    raise ConfigurationError(f"Unknown backend {name!r}. Choose from: {', '.join(BACKEND_NAMES)}.")


def build_router(
    names: list[str] | None = None,
    *,
    transport_name: str = "cli",
    auth_mode: str = "adc",
    colab_bin: str = "colab",
) -> BackendRouter:
    """Build a router over the named backends (default: all), in failover order."""
    selected = names or list(BACKEND_NAMES)
    backends = [
        build_backend(n, transport_name=transport_name, auth_mode=auth_mode, colab_bin=colab_bin)
        for n in selected
    ]
    return BackendRouter(backends, order=selected)
