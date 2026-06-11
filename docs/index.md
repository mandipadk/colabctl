# colabctl

Programmatic control of **Google Colab** for developers and AI agents — allocate
GPU/TPU runtimes, run code and notebooks, stream outputs, and sync files, **without
ever touching the Colab website manually.**

## Install

```bash
# library (add the extras you need)
pip install colabctl
pip install "colabctl[cli,sdk,native,secrets]"

# or as a CLI tool — exposes `colabctl` and `colabctl-mcp`
uv tool install "colabctl[cli,sdk]"
```

Bleeding edge: `pip install "colabctl[all] @ git+https://github.com/mandipadk/colabctl.git"`.

Optional extras: `cli`, `sdk`, `native`, `secrets`, `mcp`, `drive`, `modal`, `vertex`,
`hf`, `browser` (or `all`).

## Quickstart (SDK)

```python
import asyncio
from colabctl import ColabClient

async def main():
    async with ColabClient() as colab:                       # sanctioned CLI transport by default
        async with await colab.allocate(gpu="T4") as gpu:
            r = await gpu.run("import torch; print(torch.cuda.get_device_name(0))")
            print(r.text)

asyncio.run(main())
```

## Quickstart (CLI)

```bash
colabctl run train.py --gpu A100,L4,T4         # one-shot with a fallback ladder
colabctl quota                                 # compute-unit balance + burn rate
colabctl job run train.py --backend modal --gpu A100 --req torch

# durable detached job — survives your client exiting / a disconnect / reclamation:
id=$(colabctl -t native job run train.py --detach --resumable --gpu A100,L4,T4)
colabctl -t native job logs -f "$id"           # follow; resumes exactly after a disconnect
colabctl -t native job result "$id"
```

`-t native` opts into the from-scratch transport; `-t browser` drives a Colab notebook
through Colab's own MCP tools via a logged-in tab (sanctioned, and the one path that keeps
its runtime alive).

## For AI agents (MCP)

```json
{ "mcpServers": { "colabctl": { "command": "colabctl-mcp" } } }
```

Exposes interactive Colab tools (`allocate_runtime`, `run_code`, `interrupt_runtime`) plus
the submit→poll job set (`submit_job`, `job_status`, `job_logs`, `job_result`, `cancel_job`)
and `run_job` / `list_backends` across the Colab, Modal, and Vertex backends.

## Authentication

The Colab paths use Google Application Default Credentials — **one-time per machine**:

```bash
colabctl auth login     # runs the gcloud ADC login with the scopes colabctl needs
colabctl auth status    # account · scopes · Drive quota project · what to fix
```

For **runtime-direct Drive checkpoints**, ADC user credentials also need a quota project
with the Drive API enabled (`gcloud auth application-default set-quota-project YOUR_PROJECT`;
`auth status` flags it, and colabctl auto-detects it).

See [Architecture](architecture.md) for how the pieces fit together.
