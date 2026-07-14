# colabctl

colabctl controls Google Colab and other GPU providers from Python, the terminal, or an AI
agent. It supports interactive runtimes, parameterized notebooks, detached Colab processes, and
batch jobs across Colab, Modal, Vertex AI, Hugging Face Jobs, Kaggle, RunPod, and Vast.ai.

The project is in active development. Check the
[public roadmap](https://github.com/mandipadk/colabctl/blob/main/ROADMAP.md) before depending on a
backend's live-validation or durability level.

## Install

```bash
uv tool install "colabctl[cli,sdk]"
# or install every optional integration
uv tool install "colabctl[all]"
```

Python 3.12 or newer is required. Optional extras are `cli`, `sdk`, `native`, `browser`, `drive`,
`secrets`, `mcp`, `modal`, `vertex`, `hf`, `kaggle`, and `runpod`.

## Authenticate

```bash
colabctl auth login
colabctl auth status
colabctl doctor
```

Drive operations also need a Google Cloud quota project with the Drive API enabled. See
[Deployment and operations](deployment.md) for the exact setup.

## Python SDK

```python
import asyncio
from colabctl import ColabClient

async def main():
    async with ColabClient() as colab:
        async with await colab.allocate(gpu="T4") as gpu:
            result = await gpu.run(
                "import torch; print(torch.cuda.get_device_name(0))"
            )
            print(result.text)

asyncio.run(main())
```

## CLI

```bash
colabctl run train.py --gpu A100,L4,T4
colabctl new --gpu T4 --name experiment
colabctl exec --session experiment --code "print(2**10)"
colabctl stop experiment

colabctl job run train.py --backend modal --gpu A100 --req torch
colabctl job backends
```

## Custom Colab transport

The official CLI transport is the default. Enable the custom transport when you need its
cross-process session and detached-job features:

```bash
export COLABCTL_ENABLE_NATIVE=1
colabctl --transport native new --gpu T4 --name experiment
colabctl --transport native attach experiment
```

Detached jobs continue on the same runtime after the submitting shell disconnects. A later poll
can relaunch a resumable job after runtime loss, but the application must save and restore its
own external checkpoint. See [Deployment and operations](deployment.md) for the complete
limitations.

## AI agents

Install the MCP integration with `uv tool install "colabctl[cli,mcp]"`, then configure the
local server:

```json
{
  "mcpServers": {
    "colabctl": {
      "command": "colabctl-mcp"
    }
  }
}
```

The server exposes runtime allocation, code execution, file operations, and batch-job tools.
It runs with the filesystem and provider credentials of its process.

## Continue reading

- [Examples](examples.md)
- [Backends](backends.md)
- [Architecture](architecture.md)
- [API reference](api.md)
- [Deployment and operations](deployment.md)
