"""colabctl Hydra launcher plugin (Phase 4.10.5, part 4).

Tests the hydra-free core (the job-runner builder) + the critical namespace-package layout.
The launcher itself imports hydra and submits live Colab jobs, so its end-to-end path is
validated with hydra + a real account (like the spot backends), not in the hermetic suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins" / "colabctl-hydra-launcher"


def test_plugin_namespace_package_layout():
    base = _PLUGIN_ROOT / "hydra_plugins" / "colabctl_launcher"
    assert (base / "launcher.py").is_file()
    assert (base / "config.py").is_file()
    assert (base / "_spec.py").is_file()
    assert (base / "__init__.py").is_file()  # the subpackage HAS __init__.py
    # CRITICAL (Hydra plugin discovery): NO __init__.py at the hydra_plugins top level.
    assert not (_PLUGIN_ROOT / "hydra_plugins" / "__init__.py").exists()


def test_build_job_code_is_valid_and_embeds_payload():
    sys.path.insert(0, str(_PLUGIN_ROOT))
    try:
        from hydra_plugins.colabctl_launcher._spec import build_job_code  # hydra-free
    finally:
        sys.path.remove(str(_PLUGIN_ROOT))
    code = build_job_code("UEFZTE9BRA==")
    compile(code, "<job>", "exec")  # valid Python
    assert "cloudpickle.loads" in code and "OmegaConf.create" in code
    assert "UEFZTE9BRA==" in code  # the cloudpickled (task, config) payload is embedded
