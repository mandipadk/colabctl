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
colabctl run train.py --gpu T4                 # one-shot: allocate → run → release
colabctl job run train.py --backend modal --gpu A100 --req torch
colabctl job backends                          # list backends + capabilities
```

## For AI agents (MCP)

```json
{ "mcpServers": { "colabctl": { "command": "colabctl-mcp" } } }
```

Exposes interactive Colab tools plus `run_job` / `list_backends` across the Colab,
Modal, and Vertex backends.

## Authentication

The sanctioned Colab path uses Application Default Credentials:

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/userinfo.email,\
https://www.googleapis.com/auth/colaboratory,\
https://www.googleapis.com/auth/drive.file
```

See [Architecture](architecture.md) for how the pieces fit together.
