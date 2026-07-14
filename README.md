# colabctl

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org)
[![CI](https://github.com/mandipadk/colabctl/actions/workflows/ci.yml/badge.svg)](https://github.com/mandipadk/colabctl/actions/workflows/ci.yml)

Control Google Colab and other GPU providers from Python, the terminal, or an AI agent.
colabctl allocates runtimes, executes scripts and notebooks, streams output, moves files,
and exposes a common batch-job API across seven backends.

```python
import asyncio
from colabctl import ColabClient

async def main():
    async with ColabClient() as colab:
        async with await colab.allocate(gpu="A100,L4,T4") as gpu:
            result = await gpu.run(
                "import torch; print(torch.cuda.get_device_name(0))"
            )
            print(result.text)

asyncio.run(main())
```

## Project status

colabctl is in active development. The default Colab transport uses Google's official
`google-colab-cli`. The custom `native` transport, durable sessions, file transfer, Drive
checkpoint helpers, and Modal backend have also been checked against real accounts. Vertex AI,
Hugging Face Jobs, Kaggle, RunPod, and Vast.ai are implemented and covered by hermetic tests;
their public status remains test-only until each live validation is repeated and documented.

See [ROADMAP.md](./ROADMAP.md) for the current support matrix and known limitations.

## Install

colabctl requires Python 3.12 or newer. Install the CLI and SDK with `uv`:

```bash
uv tool install "colabctl[cli,sdk]"
```

Install the package as a library with `pip`:

```bash
pip install "colabctl[cli,sdk]"
```

Add the features you use, or install everything:

```bash
uv tool install "colabctl[all]"
```

Available extras are `cli`, `sdk`, `native`, `browser`, `drive`, `secrets`, `mcp`, `modal`,
`vertex`, `hf`, `kaggle`, and `runpod`. Vast.ai uses the core HTTP client and needs no separate
extra.

## Authenticate with Colab

The Colab transports use Google Application Default Credentials. colabctl wraps the login and
reports the account, scopes, and Drive quota-project status:

```bash
colabctl auth login
colabctl auth status
```

Runtime-direct Drive transfers also need a quota project with the Drive API enabled:

```bash
gcloud services enable drive.googleapis.com --project=YOUR_PROJECT
gcloud auth application-default set-quota-project YOUR_PROJECT
```

Other backends use their provider credentials, such as `MODAL_TOKEN_ID` and
`MODAL_TOKEN_SECRET`, `HF_TOKEN`, `RUNPOD_API_KEY`, `VAST_API_KEY`, or a Kaggle credentials
file.

## Run code and manage sessions

```bash
colabctl doctor
colabctl run train.py --gpu A100,L4,T4
colabctl new --gpu T4 --name experiment
colabctl exec --session experiment --code "print(2**10)"
colabctl sessions
colabctl stop experiment
```

The official CLI transport is the default. The custom transport uses the CLI name `native` and
requires explicit opt-in:

```bash
export COLABCTL_ENABLE_NATIVE=1
colabctl --transport native new --gpu T4 --name experiment
colabctl --transport native attach experiment
```

Use the custom transport for cross-process attach, streaming Jupyter output, runtime file
transfer, interrupt, keep-alive, and detached Colab jobs. The browser transport connects through
a logged-in Colab tab.

## Run batch jobs

```bash
colabctl job run train.py --backend modal --gpu A100 --req torch
colabctl job run train.py --backend hf --gpu A100
colabctl job backends
```

The job API supports Colab, Modal, Vertex AI, Hugging Face Jobs, Kaggle, RunPod, and Vast.ai.
Opt-in routing can order eligible backends by the catalog price and retry typed infrastructure
failures:

```bash
colabctl job run train.py \
  --backend colab \
  --allow colab,modal,runpod,vast \
  --cheapest \
  --max-price 2.50 \
  --timeout 3600
```

Fallback re-executes the workload on another provider. Use it only for idempotent jobs. Catalog
prices and the local spend ledger are estimates; they are useful admission filters, not provider
billing guarantees.

## Detached Colab jobs

The custom transport can start a supervised process on a Colab runtime and return a job ID. The
process keeps running when the submitting shell exits or its connection drops:

```bash
export COLABCTL_ENABLE_NATIVE=1
JOB_ID=$(colabctl --transport native job run train.py --detach --resumable --gpu T4)
colabctl --transport native job status "$JOB_ID"
colabctl --transport native job logs "$JOB_ID" --follow
colabctl --transport native job result "$JOB_ID"
```

`--resumable` allows a later `status` or `result` call to detect a reclaimed runtime, allocate a
replacement, and relaunch the stored workload. The workload must write checkpoints to external
storage and restore them itself. Recovery currently depends on a client poll, and log bytes that
only existed on a reclaimed runtime may be unavailable.

## Notebooks

```bash
colabctl notebook run training.ipynb \
  --param epochs=10 \
  --gpu T4 \
  --out training-output.ipynb
```

## AI agents

Install the MCP extra and run the local server:

```bash
uv tool install "colabctl[cli,mcp]"
```

```json
{
  "mcpServers": {
    "colabctl": {
      "command": "colabctl-mcp"
    }
  }
}
```

The MCP server exposes interactive runtime tools and batch-job operations. The wheel also
contains an Agent Skill with command guidance:

```bash
colabctl skill install
```

Run the MCP server with the same care as any local arbitrary-code execution tool. Restrict its
credentials and workspace access to the account and files the agent needs.

## Backends

| Backend | Current public status | Main caveat |
|---|---|---|
| Colab | Official transport and custom transport checked live | Dynamic limits; detached recovery has the constraints above |
| Modal | Checked live | Requires a Modal account and enforces a configured timeout ceiling |
| Vertex AI | Implemented and hermetically tested | Logs remain in Cloud Logging; needs a project and staging bucket |
| Hugging Face Jobs | Implemented and hermetically tested | Requires an HF token; live validation pending |
| Kaggle | Implemented and hermetically tested | T4 only, no cancel API, end-of-run log retrieval |
| RunPod | Implemented and hermetically tested | Persist outputs outside the pod; stdout is not retained by the adapter |
| Vast.ai | Implemented and hermetically tested | Marketplace capacity and price vary by host |

## Documentation

- [Examples](./docs/examples.md)
- [Backends](./docs/backends.md)
- [Architecture](./docs/architecture.md)
- [Deployment and operations](./docs/deployment.md)
- [API reference](./docs/api.md)
- [Public roadmap](./ROADMAP.md)
- [Contributing](./CONTRIBUTING.md)

## Provider terms

The default Colab path uses Google's official CLI. The custom transport is disabled by default
and must be enabled explicitly. Google and other providers can change quotas, availability,
prices, and permitted behavior. Use your own account, avoid quota circumvention, and follow each
provider's current terms.

## License

[Apache 2.0](./LICENSE)
