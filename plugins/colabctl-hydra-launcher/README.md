# colabctl-hydra-launcher

A [Hydra](https://hydra.cc) launcher plugin that runs each `--multirun` job as a **durable
colabctl detached GPU job** — so a sweep fans out across (re-assignable, auto-resuming) Colab
runtimes instead of running locally.

It's a separate distribution from `colabctl` because Hydra discovers launchers via the
`hydra_plugins` [namespace package](https://hydra.cc/docs/advanced/plugins/develop/) (no
top-level `__init__.py`, no entry points), which is cleanest shipped on its own.

## Install

```bash
pip install colabctl-hydra-launcher   # pulls hydra-core + colabctl
```

## Use

```bash
python train.py --multirun hydra/launcher=colab \
  hydra.launcher.accelerator=A100 hydra.launcher.track=wandb \
  optimizer=adam,sgd lr=0.1,0.01,0.001
```

That launches one **detached** colabctl job per swept config (here 6). Each job's id is the
Hydra `JobReturn` value; poll them later from any shell:

```bash
colabctl job list
colabctl job status <id> ; colabctl job result <id>
colabctl audit          # lineage: which run/URL each job produced (with track=wandb|mlflow)
```

## Config (`hydra.launcher.*`)

| field | default | meaning |
|---|---|---|
| `accelerator` | `T4` | GPU per job (T4/L4/A100/H100) |
| `requirements` | `[]` | extra pip installs per runtime |
| `resumable` | `true` | each job auto-resumes across a runtime re-assign |
| `track` | `null` | `wandb`/`mlflow` experiment tracking per job |
| `poll` | `false` | block on each job's result vs fire-and-return the id |

## Status

Built against Hydra's stable Launcher plugin protocol. The hydra-free job-runner builder is
unit-tested; the end-to-end remote sweep needs hydra + a live Colab account to validate (the
task function + swept config are cloudpickled to each job, so module-level, picklable tasks work
best).
