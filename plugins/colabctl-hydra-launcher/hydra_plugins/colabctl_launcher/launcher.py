"""``hydra/launcher=colab`` — run each Hydra ``--multirun`` job as a durable colabctl detached
job, fanning the sweep out across (re-assignable) Colab runtimes.

Each swept config is cloudpickled with the task function and shipped to a detached colabctl job
(``submit`` returns immediately); the job id is the ``JobReturn`` value, so a sweep of N jobs
launches N durable jobs you poll later with ``colabctl job status/result`` (or with ``poll=True``
the launcher blocks on each result). Built against Hydra's stable Launcher plugin protocol; the
end-to-end remote sweep needs hydra + a live Colab account to validate.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Sequence

import cloudpickle
from hydra.core.utils import JobReturn, JobStatus, configure_log, filter_overrides, setup_globals
from hydra.plugins.launcher import Launcher
from hydra.types import HydraContext, TaskFunction
from hydra_plugins.colabctl_launcher._spec import build_job_code
from omegaconf import DictConfig, OmegaConf, open_dict

log = logging.getLogger(__name__)


class ColabLauncher(Launcher):
    def __init__(
        self,
        accelerator: str = "T4",
        requirements: list[str] | None = None,
        resumable: bool = True,
        track: str | None = None,
        poll: bool = False,
    ) -> None:
        self.accelerator = accelerator
        self.requirements = list(requirements or [])
        self.resumable = resumable
        self.track = track
        self.poll = poll
        self.config: DictConfig | None = None
        self.hydra_context: HydraContext | None = None
        self.task_function: TaskFunction | None = None

    def setup(
        self,
        *,
        hydra_context: HydraContext,
        task_function: TaskFunction,
        config: DictConfig,
    ) -> None:
        self.config = config
        self.hydra_context = hydra_context
        self.task_function = task_function

    def launch(
        self, job_overrides: Sequence[Sequence[str]], initial_job_idx: int
    ) -> Sequence[JobReturn]:
        setup_globals()
        assert self.config is not None
        assert self.hydra_context is not None
        assert self.task_function is not None
        configure_log(self.config.hydra.hydra_logging, self.config.hydra.verbose)

        # Lazy so the plugin imports without colabctl on the path during config discovery.
        from colabctl.backends.base import JobSpec
        from colabctl.jobs.backend import DetachedColabBackend
        from colabctl.models import Accelerator

        backend = DetachedColabBackend.create()
        runs: list[JobReturn] = []
        try:
            for idx, overrides in enumerate(job_overrides):
                job_num = initial_job_idx + idx
                lst = " ".join(filter_overrides(overrides))
                sweep_config = self.hydra_context.config_loader.load_sweep_config(
                    self.config, list(overrides)
                )
                with open_dict(sweep_config):
                    sweep_config.hydra.job.num = job_num
                    sweep_config.hydra.job.id = f"job_{job_num}"
                payload = base64.b64encode(
                    cloudpickle.dumps(
                        (self.task_function, OmegaConf.to_container(sweep_config, resolve=False))
                    )
                ).decode()
                spec = JobSpec(
                    code=build_job_code(payload),
                    accelerator=Accelerator(self.accelerator.upper()),
                    requirements=["hydra-core", "cloudpickle", *self.requirements],
                    resumable=self.resumable,
                    track=self.track,
                )
                info = asyncio.run(backend.submit(spec))
                log.info("colabctl: job #%d -> %s  [%s]", job_num, info.id, lst)

                ret = JobReturn()
                ret.cfg = sweep_config
                ret.overrides = list(overrides)
                ret.return_value = info.id  # the detached job id — poll with `colabctl job ...`
                ret.status = JobStatus.COMPLETED
                if self.poll:  # block on each job's terminal result
                    ret.return_value = asyncio.run(backend.result(info.id)).state.value
                runs.append(ret)
        finally:
            asyncio.run(backend.aclose())
        return runs
