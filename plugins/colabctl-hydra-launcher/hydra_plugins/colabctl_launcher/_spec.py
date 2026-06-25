"""Build the VM-side runner code for one Hydra sweep job.

Kept free of any ``hydra`` import so it is unit-testable on its own — the launcher (which does
import hydra) calls this to turn a cloudpickled ``(task_function, config)`` into the code a
colabctl detached job runs.
"""

from __future__ import annotations


def build_job_code(payload_b64: str) -> str:
    """Code that unpickles ``(task_function, config_container)`` and runs the task with the config.

    ``payload_b64`` is base64 of ``cloudpickle.dumps((task_function, OmegaConf-container))``. The
    runtime re-hydrates the swept config and calls the original Hydra task with it — so each
    ``--multirun`` job runs remotely with exactly its overrides.
    """
    return (
        "import base64, cloudpickle\n"
        "from omegaconf import OmegaConf\n"
        f"_fn, _cfg = cloudpickle.loads(base64.b64decode({payload_b64!r}))\n"
        "_fn(OmegaConf.create(_cfg))\n"
    )
