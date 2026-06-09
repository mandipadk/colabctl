"""Integration rig: drive the native kernel against a real local Jupyter server.

Validates the `NativeKernel` path (jupyter-kernel-client over the standard
/api/kernels + channels protocol) end-to-end **without a Colab account** — a plain
Jupyter server speaks the same protocol; the Colab-specific proxy headers/params are
harmless extras a vanilla server ignores.

Opt-in: skipped unless `COLABCTL_INTEGRATION=1`, and skipped if the integration deps
aren't installed. Run with:

    COLABCTL_INTEGRATION=1 uv run --extra native --extra integration \
        pytest tests/test_integration_kernel.py -v
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("COLABCTL_INTEGRATION"):
    pytest.skip("integration rig disabled (set COLABCTL_INTEGRATION=1)", allow_module_level=True)

pytest.importorskip("jupyter_server")
pytest.importorskip("jupyter_kernel_client")
pytest.importorskip("ipykernel")

from colabctl.transport.native.kernel import NativeKernel  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def jupyter_server():
    token = "colabctl-itest-token"
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "jupyter_server",
            f"--ServerApp.token={token}",
            f"--ServerApp.port={port}",
            "--ServerApp.ip=127.0.0.1",
            "--ServerApp.open_browser=False",
            "--ServerApp.disable_check_xsrf=True",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                req = urllib.request.Request(
                    f"{url}/api", headers={"Authorization": f"token {token}"}
                )
                with urllib.request.urlopen(req, timeout=1):
                    break
            except Exception:
                if proc.poll() is not None:
                    pytest.skip("jupyter server failed to start")
                time.sleep(0.3)
        else:
            pytest.skip("jupyter server did not come up in time")
        yield url, token
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


async def test_native_kernel_executes_on_real_jupyter(jupyter_server):
    url, token = jupyter_server
    kernel = NativeKernel(url, token)
    try:
        result = await kernel.execute("print(6 * 7)")
        assert result.ok
        assert "42" in result.text
    finally:
        await kernel.stop()


async def test_native_kernel_streams_outputs(jupyter_server):
    url, token = jupyter_server
    seen: list[str] = []
    kernel = NativeKernel(url, token)
    try:
        await kernel.execute(
            "for i in range(3): print(i)",
            on_output=lambda o: seen.append(getattr(o, "text", "")),
        )
        assert "".join(seen)  # outputs streamed via the hook
    finally:
        await kernel.stop()
