"""Config for the colabctl Hydra launcher, registered under the ``hydra/launcher`` group.

Select it with ``hydra/launcher=colab`` (or ``python app.py --multirun hydra/launcher=colab``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hydra.core.config_store import ConfigStore


@dataclass
class ColabLauncherConf:
    _target_: str = "hydra_plugins.colabctl_launcher.launcher.ColabLauncher"
    #: Accelerator for each detached job (T4/L4/A100/H100).
    accelerator: str = "T4"
    #: Extra pip requirements installed on each runtime.
    requirements: list[str] = field(default_factory=list)
    #: Mark each detached job auto-resumable across a runtime re-assign.
    resumable: bool = True
    #: Experiment tracking for each job: "wandb" | "mlflow" | null.
    track: str | None = None
    #: Block on each job's result (True) vs fire-and-return the job id (False, the default —
    #: poll later with ``colabctl job status/result``).
    poll: bool = False


ConfigStore.instance().store(
    group="hydra/launcher", name="colab", node=ColabLauncherConf, provider="colabctl"
)
